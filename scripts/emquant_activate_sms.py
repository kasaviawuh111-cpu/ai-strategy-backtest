"""Activate the local Choice Quant SDK without storing an account password."""

from __future__ import annotations

import re
from getpass import getpass

from EmQuantAPI import c

SMS_BODY = "SXDL"
SMS_DESTINATION = "9535711"


def _quiet_log(_: bytes) -> int:
    return 1


def _masked_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


def main() -> int:
    print(f"请先用已绑定 Choice API 账号的手机号发送“{SMS_BODY}”到 {SMS_DESTINATION}。")
    print("短信验证码模式在发送后 10 分钟内有效；手机号只在本次进程内使用。")
    phone = getpass("请输入已绑定手机号（输入不会显示）：").strip()
    if re.fullmatch(r"1\d{10}", phone) is None:
        print("手机号格式不正确，未发起登录。")
        return 2

    print(f"正在为 {_masked_phone(phone)} 激活本机令牌……")
    result = c.start(
        f"LoginMode=SXDL,PhoneNumber={phone},ForceLogin=0,RecordLoginInfo=0",
        _quiet_log,
    )
    if result.ErrorCode != 0:
        print(f"激活失败：{result.ErrorCode} {result.ErrorMsg}")
        return 1

    try:
        print("激活成功。SDK 已生成本机 userInfo 令牌，后续不需要保存账号密码。")
        print("下一步运行：.venv/bin/python scripts/emquant_probe.py")
        return 0
    finally:
        c.stop()


if __name__ == "__main__":
    raise SystemExit(main())
