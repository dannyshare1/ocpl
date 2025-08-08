# -*- coding: utf-8 -*-
"""
OCI A1 (ARM) Always Free 自动蹲位脚本
- 轮询 AD-1/2/3，按 4→2→1 OCPU 递减重试
- 自动/手动选择 Ubuntu 22.04 ARM 镜像（.env 的 IMAGE_OCID 优先生效）
- 成功后输出实例 OCID & 公网 IP，并写入 SUCCESS.txt
- 支持 Telegram 通知（可选）
- 重要：COMPARTMENT_OCID 建议填 tenancy 根 OCID；SUBNET_OCID 填公共子网 OCID
"""

import base64
import os
import sys
import time
import traceback
from typing import List, Optional

import oci
from dotenv import load_dotenv

# ---------- 环境变量 ----------
load_dotenv()

CONFIG_FILE = os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config"))
PROFILE = os.getenv("OCI_PROFILE", "DEFAULT")

COMPARTMENT_OCID = os.getenv("COMPARTMENT_OCID")   # 建议用 tenancy 根 OCID
SUBNET_OCID = os.getenv("SUBNET_OCID")             # 公共子网 OCID
SSH_PUBLIC_KEY_PATH = os.path.expanduser(os.getenv("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_rsa.pub"))

IMAGE_OCID_OVERRIDE = (os.getenv("IMAGE_OCID") or "").strip()

INSTANCE_NAME_PREFIX = os.getenv("INSTANCE_NAME_PREFIX", "a1-free")
BOOT_VOLUME_GB = int(os.getenv("BOOT_VOLUME_GB", "50"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "120"))

ADS = [s.strip() for s in os.getenv("ADS", "AD-1,AD-2,AD-3").split(",") if s.strip()]
OCPU_CANDIDATES = [int(x) for x in os.getenv("OCPUS", "4,2,1").split(",") if x.strip().isdigit()]
MEM_PER_OCPU = int(os.getenv("MEM_PER_OCPU", "6"))  # A1.Flex 1~6 GB/核

TG_BOT_TOKEN = (os.getenv("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.getenv("TG_CHAT_ID") or "").strip()

# ---------- cloud-init（可替换为你的 WordPress/LNMP） ----------
CLOUD_INIT = """#cloud-config
package_update: true
packages: [curl, htop]
runcmd:
  - timedatectl set-timezone Asia/Shanghai || true
  - ufw disable || true
"""

# ---------- 通知 ----------
def notify(msg: str):
    print(msg, flush=True)
    if TG_BOT_TOKEN and TG_CHAT_ID:
        try:
            import requests  # 可选依赖
            requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": msg},
                timeout=10
            )
        except Exception:
            traceback.print_exc()

# ---------- OCI clients ----------
def get_clients():
    cfg = oci.config.from_file(CONFIG_FILE, PROFILE)
    compute = oci.core.ComputeClient(cfg)
    vcn = oci.core.VirtualNetworkClient(cfg)
    iam = oci.identity.IdentityClient(cfg)
    return cfg, compute, vcn, iam

def list_availability_domains(iam, compartment_id) -> List[str]:
    try:
        ads = iam.list_availability_domains(compartment_id).data
        return [ad.name for ad in sorted(ads, key=lambda x: x.name)]
    except Exception:
        return ADS

# ---------- 镜像选择 ----------
def pick_latest_ubuntu_arm_image(compute, compartment_id) -> Optional[str]:
    pages = oci.pagination.list_call_get_all_results(
        compute.list_images,
        compartment_id=compartment_id,
        operating_system="Canonical Ubuntu",
        sort_by="TIMECREATED",
        sort_order="DESC"
    ).data
    for img in pages:
        name = (img.display_name or "").lower()
        if "22.04" in name and ("aarch64" in name or "arm" in name):
            return img.id
    return None

# ---------- 创建实例 ----------
def try_launch(compute, vcn, compartment_id, image_id, ad_name, ocpus, mem_gb):
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
        boot_volume_size_in_gbs=BOOT_VOLUME_GB,   # ← 正确位置
    )

    display_name = f"{INSTANCE_NAME_PREFIX}-ad{ad_name[-1]}-{ocpus}c{mem_gb}g"

    launch = LaunchInstanceDetails(
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

        # 等待 RUNNING
        oci.wait_until(
            compute,
            compute.get_instance(inst_id),
            "lifecycle_state",
            "RUNNING",
            max_wait_seconds=900,
            succeed_on_not_found=False,
        )

        # 取公网 IP
        atts = vcn.list_vnic_attachments(compartment_id, instance_id=inst_id).data
        if not atts:
            raise RuntimeError("未找到 VNIC 附着")
        vnic = vcn.get_vnic(atts[0].vnic_id).data
