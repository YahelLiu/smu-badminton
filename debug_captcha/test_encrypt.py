"""离线交叉验证 captchaCode AES 加密(无需 token / 无网络 / 无副作用)。

用一组真实的 (captcha_id, captchaCode) —— 取自一次 live checkCaptcha 成功响应 ——
跑 booking_api.encrypt_captcha_code, 然后:
  1. 跨库 round-trip: pycryptodome 加密 -> cryptography 解密(或反过来), 应还原原 captchaCode。
  2. openssl oracle: 用 openssl enc -aes-128-cbc 以同样的 key/iv 算一遍, 与 Python 输出逐字符比对。
     openssl 与 pycryptodome / cryptography / crypto-js 是各自独立实现的标准 AES, 一致
     => 强证据: 我们复现的就是 crypto-js w() 的输出(标准 AES-128-CBC, PKCS7, base64)。

默认 fixture(用户已跑出的 live 值, 见 show_captcha_code.py 输出):
  captcha_id  = cc1973dc4eaf4132b89d6d162793fc97          (checkCaptcha 响应 data.captchaId, 无 SLIDER_ 前缀)
  captchaCode = 3ed524c3-11b8-4746-8527-d6ec2fc94283      (36 字符 uuid -> 必走加密分支)

用法:
  python debug_captcha/test_encrypt.py
  python debug_captcha/test_encrypt.py --captcha-id <id> --captcha-code <code>   # 用新一对再验
"""
from __future__ import annotations

import argparse
import base64
import logging
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from smu_badminton.booking_api import encrypt_captcha_code  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("enc-test")

# live fixture: 来自一次真实 checkCaptcha 成功响应(show_captcha_code.py 跑出的)
FIXTURE_ID = "cc1973dc4eaf4132b89d6d162793fc97"
FIXTURE_CODE = "3ed524c3-11b8-4746-8527-d6ec2fc94283"


def derive_key_iv(captcha_id: str, captcha_code: str) -> tuple[bytes, bytes, str]:
    """复刻 encrypt_captcha_code 的 key/iv 派生, 返回 (key, iv, 实际用的 cid)。"""
    assert len(captcha_code) >= 16, "captchaCode <16 不会走加密分支"
    cid = captcha_id[len("SLIDER_"):] if captcha_id.startswith("SLIDER_") else captcha_id
    assert len(cid) >= 17, f"captcha_id 过短, 无法派生 16 字节 key/iv: {cid!r}"
    key = cid[:16].encode("utf-8")
    iv = cid[1:17].encode("utf-8")
    return key, iv, cid


def aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-128-CBC + PKCS7 解密(跨库回退, 与 _aes_cbc_encrypt 对称)。"""
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext), AES.block_size)
    except ImportError:
        pass
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as _pad
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = _pad.PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def openssl_oracle(key: bytes, iv: bytes, plaintext: str):
    """用 openssl enc 独立计算 AES-128-CBC base64; 不可用返回 None。"""
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    # -K / -iv 接 hex; -base64 -A -> 单行 base64; stdin 喂明文(无尾随换行)
    cmd = [openssl, "enc", "-aes-128-cbc",
           "-K", key.hex(), "-iv", iv.hex(),
           "-base64", "-A"]
    try:
        proc = subprocess.run(cmd, input=plaintext.encode("utf-8"),
                              capture_output=True, timeout=10)
    except Exception as e:  # noqa: BLE001
        logger.warning("openssl 调用异常: %s", e)
        return None
    if proc.returncode != 0:
        logger.warning("openssl 失败(rc=%d): %s",
                       proc.returncode, proc.stderr.decode(errors="replace").strip())
        return None
    return proc.stdout.decode("ascii").strip()


def main():
    ap = argparse.ArgumentParser(description="离线交叉验证 captchaCode AES 加密")
    ap.add_argument("--captcha-id", default=FIXTURE_ID, help="checkCaptcha 响应的 captchaId")
    ap.add_argument("--captcha-code", default=FIXTURE_CODE, help="checkCaptcha 响应的 captchaCode(明文)")
    args = ap.parse_args()

    cid, code = args.captcha_id, args.captcha_code
    logger.info("captcha_id  = %s", cid)
    logger.info("captchaCode = %s (len=%d)", code, len(code))
    if len(code) < 16:
        logger.error("captchaCode <16, 不走加密分支, 无可验证; 退出。")
        return

    key, iv, cid_used = derive_key_iv(cid, code)
    logger.info("派生 key=%r (hex=%s)", key, key.hex())
    logger.info("派生 iv =%r (hex=%s)", iv, iv.hex())
    logger.info("实际用 cid=%s", cid_used)

    # 1) Python 加密(encrypt_captcha_code 内部走 pycryptodome / cryptography 回退链)
    enc = encrypt_captcha_code(cid, code)
    logger.info("Python 加密结果: %s", enc)
    if not enc or enc == code or len(enc) < 16:
        logger.error("加密未生效! 多半缺加密库, encrypt_captcha_code 回退原样直传。")
        logger.error("请先装: uv pip install pycryptodome")
        sys.exit(1)

    # 2) 跨库 round-trip: 解密应还原原 captchaCode
    try:
        ciphertext = base64.b64decode(enc)
        pt = aes_cbc_decrypt(key, iv, ciphertext)
        recovered = pt.decode("utf-8")
    except Exception as e:  # noqa: BLE001
        logger.error("解密异常: %s", e)
        sys.exit(1)
    ok_roundtrip = (recovered == code)
    logger.info("round-trip 解密还原: %s  -> %r",
                "OK" if ok_roundtrip else "FAIL", recovered)
    if not ok_roundtrip:
        logger.error("round-trip 失败! 还原=%r 期望=%r", recovered, code)
        sys.exit(1)

    # 3) openssl 独立 oracle(与 pycryptodome 各自独立实现; 一致 = 强证据匹配 crypto-js)
    ossl = openssl_oracle(key, iv, code)
    if ossl is None:
        logger.warning("未找到 openssl, 跳过独立 oracle(round-trip 已通过)。")
        ok_oracle = None
    else:
        logger.info("openssl   输出:    %s", ossl)
        ok_oracle = (ossl == enc)
        logger.info("openssl == Python:  %s", "OK ✓" if ok_oracle else "FAIL ✗")
        if not ok_oracle:
            logger.error("openssl 与 Python 不一致! openssl=%r python=%r", ossl, enc)
            sys.exit(1)

    # 汇总
    logger.info("=" * 60)
    verdict = ("Python AES-128-CBC 复现正确"
               + (" 且 openssl 独立 oracle 也一致" if ok_oracle else " (无 openssl, 仅 round-trip 通过)")
               + " => 与 crypto-js w()(标准 AES) 输出一致。")
    logger.info("结论: %s", verdict)


if __name__ == "__main__":
    main()
