import base64, os, sys, time, traceback
from typing import List, Optional

import oci
from dotenv import load_dotenv

load_dotenv()

CONFIG_FILE = os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config"))
PROFILE = os.getenv("OCI_PROFILE", "DEFAULT")
COMPARTMENT_OCID = os.getenv("COMPARTMENT_OCID")
SUBNET_OCID = os.getenv("SUBNET_OCID")
SSH_PUBLIC_KEY_PATH = os.path.expanduser(os.getenv("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_rsa.pub"))
INSTANCE_NAME_PREFIX = os.getenv("INSTANCE_NAME_PREFIX", "sg-a1-free")
BOOT_VOLUME_GB = int(os.getenv("BOOT_VOLUME_GB", "50"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "120"))

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

assert COMPARTMENT_OCID and SUBNET_OCID, "请在 .env 中填 COMPARTMENT_OCID 与 SUBNET_OCID"
assert os.path.exists(SSH_PUBLIC_KEY_PATH), f"找不到公钥: {SSH_PUBLIC_KEY_PATH}"

cloud_init = """#cloud-config
package_update: true
packages: [curl, htop]
runcmd:
  - timedatectl set-timezone Asia/Shanghai || true
  - ufw disable || true
"""

ADS = ["AD-1", "AD-2", "AD-3"]           # 你的区若没有 AD-3，可删掉
OCPU_CANDIDATES = [4, 2, 1]
MEM_PER_OCPU = 6                         # A1.Flex: 1~6 GB/核；AF 上限 6*核

def notify(msg: str):
    print(msg, flush=True)
    if TG_BOT_TOKEN and TG_CHAT_ID:
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": msg})
        except Exception:
            traceback.print_exc()

def get_clients():
    config = oci.config.from_file(CONFIG_FILE, PROFILE)
    compute = oci.core.ComputeClient(config)
    vnic = oci.core.VirtualNetworkClient(config)
    identity = oci.identity.IdentityClient(config)
    return config, compute, vnic, identity

def pick_latest_ubuntu_arm_image(compute, compartment_id) -> Optional[str]:
    # 取 Ubuntu 22.04 aarch64（ARM）最新镜像
    images = oci.pagination.list_call_get_all_results(
        compute.list_images,
        compartment_id=compartment_id,
        operating_system="Canonical Ubuntu",
        sort_by="TIMECREATED",
        sort_order="DESC"
    ).data
    for img in images:
        name = (img.display_name or "").lower()
        if "22.04" in name and ("aarch64" in name or "arm" in name):
            return img.id
    return None

def list_availability_domains(identity, compartment_id) -> List[str]:
    ads = identity.list_availability_domains(compartment_id).data
    # 统一成 AD-1/2/3 名称
    ordered = sorted(ads, key=lambda x: x.name)
    return [ad.name for ad in ordered]

def try_launch(compute, vnic, compartment_id, image_id, ad_name, ocpus, mem_gb):
    with open(SSH_PUBLIC_KEY_PATH, "r", encoding="utf-8") as f:
        pubkey = f.read().strip()

    md = {"ssh_authorized_keys": pubkey}
    exmd = {"user_data": base64.b64encode(cloud_init.encode()).decode()}

    display_name = f"{INSTANCE_NAME_PREFIX}-ad{ad_name[-1]}-{ocpus}c{mem_gb}g"

    launch_details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad_name,
        compartment_id=compartment_id,
        display_name=display_name,
        image_id=image_id,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus,
            memory_in_gbs=mem_gb
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_OCID,
            assign_public_ip=True
        ),
        metadata=md,
        extended_metadata=exmd,
        boot_volume_size_in_gbs=BOOT_VOLUME_GB
    )

    try:
        resp = compute.launch_instance(launch_details)
        instance_ocid = resp.data.id
        # 等待 RUNNING
        oci.wait_until(compute, compute.get_instance(instance_ocid), "lifecycle_state", "RUNNING",
                       max_wait_seconds=900, succeed_on_not_found=False)
        # 拿公网 IP
        vnics = vnic.list_vnic_attachments(compartment_id, instance_id=instance_ocid).data
        if not vnics:
            raise RuntimeError("未找到 VNIC")
        vnic_id = vnics[0].vnic_id
        vnic_info = vnic.get_vnic(vnic_id).data
        return instance_ocid, vnic_info.public_ip
    except oci.exceptions.ServiceError as e:
        # 容量错误关键字
        cap = any(k in (e.message or "").lower() for k in [
            "capacity", "outofhostcapacity", "insufficient", "out of capacity"
        ])
        raise RuntimeError("CAPACITY" if cap else f"API:{e.message}")
    except Exception as e:
        raise

def main():
    config, compute, vnic, identity = get_clients()
    region = config["region"]
    notify(f"开始蹲位：区域={region}")

    # 检索 AD
    try:
        real_ads = list_availability_domains(identity, COMPARTMENT_OCID)
        # 将 ADS 顺序按实际存在的过滤并排序
        ad_order = [ad for ad in ADS if any(ad in x for x in real_ads)]
        if not ad_order:
            ad_order = [real_ads[0]]
    except Exception:
        ad_order = ADS

    # 查镜像
    image_id = pick_latest_ubuntu_arm_image(compute, COMPARTMENT_OCID)
    if not image_id:
        notify("未找到 Ubuntu 22.04 ARM 镜像，请手动填 IMAGE_OCID 或放宽查询逻辑。")
        sys.exit(1)

    try_count = 0
    while True:
        for ad in ad_order:
            for ocpu in OCPU_CANDIDATES:
                mem = ocpu * MEM_PER_OCPU
                try_count += 1
                notify(f"[{try_count}] 尝试创建：AD={ad}  {ocpu} OCPU / {mem} GB")
                try:
                    inst_ocid, public_ip = try_launch(compute, vnic, COMPARTMENT_OCID, image_id, ad, ocpu, mem)
                    notify(f"✅ 成功！实例：{inst_ocid}\n公网IP：{public_ip}")
                    with open("SUCCESS.txt", "w", encoding="utf-8") as f:
                        f.write(f"{inst_ocid}\n{public_ip}\n")
                    return
                except RuntimeError as e:
                    if str(e) == "CAPACITY":
                        notify(f"⚠️ 容量不足：AD={ad}  {ocpu}c/{mem}g，{SLEEP_SECONDS}s 后重试…")
                        time.sleep(SLEEP_SECONDS)
                        continue
                    else:
                        notify(f"❌ 其他错误（将继续重试）：{e}")
                        time.sleep(SLEEP_SECONDS)
                        continue
                except Exception as e:
                    notify(f"❌ 异常（将继续重试）：{e}")
                    traceback.print_exc()
                    time.sleep(SLEEP_SECONDS)
        # 一轮跑完，稍作休息
        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    main()
