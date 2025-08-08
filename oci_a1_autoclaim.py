# -*- coding: utf-8 -*-
"""
OCI A1 (ARM) Always Free 自动蹲位脚本（带环境自检）
"""

import base64
import os
import sys
import time
import traceback
from typing import List, Optional

import oci
from dotenv import load_dotenv

load_dotenv()

CONFIG_FILE = os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config"))
PROFILE = os.getenv("OCI_PROFILE", "DEFAULT")

COMPARTMENT_OCID = os.getenv("COMPARTMENT_OCID")
SUBNET_OCID = os.getenv("SUBNET_OCID")
SSH_PUBLIC_KEY_PATH = os.path.expanduser(os.getenv("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_rsa.pub"))

IMAGE_OCID_OVERRIDE = (os.getenv("IMAGE_OCID") or "").strip()

INSTANCE_NAME_PREFIX = os.getenv("INSTANCE_NAME_PREFIX", "a1-free")
BOOT_VOLUME_GB = int(os.getenv("BOOT_VOLUME_GB", "50"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "120"))

ADS = [s.strip() for s in os.getenv("ADS", "AD-1,AD-2,AD-3").split(",") if s.strip()]
OCPU_CANDIDATES = [int(x) for x in os.getenv("OCPUS", "4,2,1").split(",") if x.strip().isdigit()]
MEM_PER_OCPU = int(os.getenv("MEM_PER_OCPU", "6"))

TG_BOT_TOKEN = (os.getenv("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.getenv("TG_CHAT_ID") or "").strip()

CLOUD_INIT = """#cloud-config
package_update: true
packages: [curl, htop]
runcmd:
  - timedatectl set-timezone Asia/Shanghai || true
  - ufw disable || true
"""

def notify(msg: str):
    print(msg, flush=True)
    if TG_BOT_TOKEN and TG_CHAT_ID:
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": msg},
                timeout=10
            )
        except Exception:
            traceback.print_exc()

def get_clients():
    cfg = oci.config.from_file(CONFIG_FILE, PROFILE)
    compute = oci.core.ComputeClient(cfg)
    network = oci.core.VirtualNetworkClient(cfg)
    iam = oci.identity.IdentityClient(cfg)
    return cfg, compute, network, iam

def list_availability_domains(iam, compartment_id) -> List[str]:
    try:
        ads = iam.list_availability_domains(compartment_id).data
        return [ad.name for ad in sorted(ads, key=lambda x: x.name)]
    except Exception:
        return ADS

def pick_latest_ubuntu_arm_image(compute, compartment_id) -> Optional[str]:
    """优先 22.04，找不到再回退 24.04；匹配 aarch64/arm 关键字"""
    imgs = oci.pagination.list_call_get_all_results(
        compute.list_images,
        compartment_id=compartment_id,
        operating_system="Canonical Ubuntu",
        sort_by="TIMECREATED",
        sort_order="DESC"
    ).data

    def pick(ver: str) -> Optional[str]:
        for img in imgs:
            name = (img.display_name or "").lower()
            if ver in name and ("aarch64" in name or "arm" in name):
                return img.id
        return None

    return pick("22.04") or pick("24.04")

def validate_environment(cfg, compute, network, iam):
    """在尝试创建实例前做全面自检，定位 404/权限错误的根因"""
    region = cfg.get("region")
    notify(f"区域(region) = {region}")
    # 1) 当前 API key 所属用户/租户
    try:
        whoami = iam.get_user(cfg["user"]).data
        notify(f"当前用户: {whoami.name} ({whoami.id})")
    except Exception as e:
        raise RuntimeError(f"无法读取当前用户信息，请检查 OCI 配置/密钥是否正确：{e}")

    # 2) compartment 是否存在且可见
    try:
        comp = iam.get_compartment(COMPARTMENT_OCID).data
        notify(f"Compartment: {comp.name} ({comp.id}) - 状态: {comp.lifecycle_state}")
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            raise RuntimeError("找不到 COMPARTMENT_OCID（不在本租户/本区域可见），或无读取权限（需要 'read tenancy' 或对该 compartment 的 read 权限）。")
        raise

    # 3) subnet 是否存在（且通常是区域级资源，必须在同一个 region）
    try:
        subnet = network.get_subnet(SUBNET_OCID).data
        notify(f"Subnet: {subnet.display_name} ({subnet.id}) - VCN: {subnet.vcn_id}")
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            raise RuntimeError("找不到 SUBNET_OCID：请确认该 Subnet 位于当前 region，且你有 use virtual-network-family 权限。")
        raise

    # 4) image 是否存在（若提供了 override）
    if IMAGE_OCID_OVERRIDE:
        try:
            img = compute.get_image(IMAGE_OCID_OVERRIDE).data
            notify(f"指定镜像 OK: {img.display_name} ({img.id})")
        except oci.exceptions.ServiceError as e:
            if e.status == 404:
                raise RuntimeError("找不到 IMAGE_OCID：很可能是别的 region 的镜像 ID；建议留空由脚本自动选择。")
            raise

    # 5) 权限建议：多数 404 实为权限不足
    #   - manage instance-family + use virtual-network-family in compartment
    #   - read tenancy (便于读取 AD、compartment 信息)
    notify(
        "权限提示：若仍报 404/授权失败，请为你所在的 Group 在目标 compartment 配置：\n"
        "  allow group <YourGroup> to manage instance-family in compartment <YourCompartment>\n"
        "  allow group <YourGroup> to use virtual-network-family in compartment <YourCompartment>\n"
        "  allow group <YourGroup> to read tenancy\n"
        "并确认 Subnet/Compartment/镜像与当前 region 一致。"
    )

def try_launch(compute, network, compartment_id, image_id, ad_name, ocpus, mem_gb):
    from oci.core.models import (
        LaunchInstanceDetails,
        LaunchInstanceShapeConfigDetails,
        CreateVnicDetails,
        InstanceSourceViaImageDetails,
    )

    with open(SSH_PUBLIC_KEY_PATH, "r", encoding="utf-8") as f:
        ssh_pub = f.read().strip()

    md = {"ssh_authorized_keys": ssh_pub}
    exmd = {"user_data": base64.b64encode(CLOUD_INIT.encode()).decode()}

    source = InstanceSourceViaImageDetails(
        image_id=image_id,
        boot_volume_size_in_gbs=BOOT_VOLUME_GB,
    )

    display_name = f"{INSTANCE_NAME_PREFIX}-ad{ad_name[-1]}-{ocpus}c{mem_gb}g"

    launch = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad_name,
        compartment_id=compartment_id,
        display_name=display_name,
        shape="VM.Standard.A1.Flex",
        shape_config=LaunchInstanceShapeConfigDetails(
            ocpus=ocpus,
            memory_in_gbs=mem_gb
        ),
        create_vnic_details=CreateVnicDetails(
            subnet_id=SUBNET_OCID,
            assign_public_ip=True
        ),
        metadata=md,
        extended_metadata=exmd,
        source_details=source
    )

    try:
        resp = compute.launch_instance(launch)
        inst_id = resp.data.id

        oci.wait_until(
            compute,
            compute.get_instance(inst_id),
            "lifecycle_state",
            "RUNNING",
            max_wait_seconds=900,
            succeed_on_not_found=False,
        )

        atts = compute.list_vnic_attachments(
            compartment_id=compartment_id,
            instance_id=inst_id
        ).data
        if not atts:
            raise RuntimeError("未找到 VNIC 附着")
        vnic = network.get_vnic(atts[0].vnic_id).data
        return inst_id, vnic.public_ip

    except oci.exceptions.ServiceError as e:
        msg = (e.message or "").lower()
        # 404、授权失败等
        if e.status == 404 or "notauthorized" in msg or "not found" in msg:
            raise RuntimeError("AUTH_OR_NOTFOUND: 请检查 COMPARTMENT_OCID / SUBNET_OCID / IMAGE_OCID 是否在本 region 且你有权限。")
        if any(k in msg for k in ["capacity", "outofhostcapacity", "out of capacity", "insufficient"]):
            raise RuntimeError("CAPACITY")
        raise RuntimeError(f"API:{e.status} {e.code} {e.message}")
    except Exception as e:
        raise RuntimeError(f"EX:{type(e).__name__}: {e}")

def main():
    miss = []
    if not COMPARTMENT_OCID: miss.append("COMPARTMENT_OCID")
    if not SUBNET_OCID: miss.append("SUBNET_OCID")
    if miss:
        print("缺少必要环境变量：", ", ".join(miss))
        sys.exit(1)

    if not os.path.exists(SSH_PUBLIC_KEY_PATH):
        print(f"找不到 SSH 公钥：{SSH_PUBLIC_KEY_PATH}")
        sys.exit(1)

    cfg, compute, network, iam = get_clients()
    region = cfg["region"]
    notify(f"开始蹲位：区域={region}")

    # 先做自检，把 404/权限问题前置暴露
    try:
        validate_environment(cfg, compute, network, iam)
    except Exception as e:
        notify(f"❌ 环境自检失败：{e}")
        traceback.print_exc()
        sys.exit(1)

    real_ads = list_availability_domains(iam, COMPARTMENT_OCID)
    ad_order = [ad for ad in ADS if any(ad == r for r in real_ads)] or real_ads or ADS

    image_id = IMAGE_OCID_OVERRIDE or pick_latest_ubuntu_arm_image(compute, COMPARTMENT_OCID)
    if not image_id:
        notify("未找到 Ubuntu 22.04/24.04 ARM 镜像。建议在 Secrets 里设置 IMAGE_OCID。")
        sys.exit(0)  # 正常结束，留 run.log

    attempt = 0
    while True:
        for ad in ad_order:
            for ocpu in OCPU_CANDIDATES:
                mem = ocpu * MEM_PER_OCPU
                attempt += 1
                notify(f"[{attempt}] 尝试创建：AD={ad}  {ocpu} OCPU / {mem} GB")
                try:
                    inst, ip = try_launch(compute, network, COMPARTMENT_OCID, image_id, ad, ocpu, mem)
                    notify(f"✅ 成功！实例：{inst}\n公网 IP：{ip}")
                    with open("SUCCESS.txt", "w", encoding="utf-8") as f:
                        f.write(f"{inst}\n{ip}\n")
                    return
                except RuntimeError as e:
                    msg = str(e)
                    if msg == "CAPACITY":
                        notify(f"⚠️ 容量不足（{ad} {ocpu}c/{mem}g），{SLEEP_SECONDS}s 后继续…")
                        time.sleep(SLEEP_SECONDS)
                    elif msg.startswith("AUTH_OR_NOTFOUND"):
                        notify("❌ 授权/资源不可见问题：请检查 region/OCID/权限（见上方自检提示），脚本将暂停。")
                        return
                    else:
                        notify(f"❌ 其他错误：{e}\n{SLEEP_SECONDS}s 后继续…")
                        time.sleep(SLEEP_SECONDS)
                except Exception as e:
                    notify(f"❌ 异常：{e}\n{SLEEP_SECONDS}s 后继续…")
                    traceback.print_exc()
                    time.sleep(SLEEP_SECONDS)
        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    main()
