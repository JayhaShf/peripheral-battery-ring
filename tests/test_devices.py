# -*- coding: utf-8 -*-
"""tests/test_devices.py —— 设备层的解析单测（不需要真实设备）。

全部用**固定字节串**验证解码逻辑，覆盖：

* ROG OMNI 配对列表 / 键盘 12 01 / 鼠标 12 07（含 FF AA 无应答、0–4 档 ×25）；
* 罗技 HID++ 配对短名、找特性、四种电量特性（0x1004/0x1000/0x1001/0x1F20）、
  错误回复（0x8F / 0xFF）、设备名分段读取的拼接；
* 电压 → 百分比曲线；
* 标准 HID 电量的数值换算；
* reader 的 demo 数据与"永不抛异常"约定。

运行::

    S:\\AnConda\\python.exe -m unittest discover -s tests -v
    S:\\AnConda\\python.exe tests\\test_devices.py
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from typing import Optional
from unittest import mock

# 允许直接 `python tests/test_devices.py`（把项目根目录加进 sys.path）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from devices import generic, hidiface, legacy, logitech, reader, rog  # noqa: E402
from models import DeviceInfo, Reading  # noqa: E402


def hexbytes(text: str) -> bytes:
    """把 ``"01 a0 00"`` 这样的十六进制字符串转成 bytes。"""
    return bytes.fromhex(text.replace(" ", ""))


# --------------------------------------------------------------------------
# ROG
# --------------------------------------------------------------------------
class TestRogParsing(unittest.TestCase):
    """ROG OMNI 协议解析（真机回复原样作为测试夹具）。"""

    # 真机回复：配对列表，[5:] = 25 1c 02 04 → PID 0x1C25，遇 0 结束
    PAIRED_REPLY = hexbytes(
        "01 a0 00 01 00 25 1c 02 04 00 00 00 00 00 00 00 00 00 00 00"
    )
    # 真机回复：键盘电量 62%，未充电
    KEYBOARD_REPLY = hexbytes(
        "02 12 01 00 00 00 3e 03 01 00 14 3e 3c 0f 00 00 00 00 00 00"
    )
    # 真机回复：鼠标通道无设备
    NO_ANSWER_REPLY = hexbytes("03 ff aa 00 00 00 00 00 00 00 00 00 00 00 00 00")

    def test_parse_paired_pids_single(self) -> None:
        """配对列表解析出 0x1C25。"""
        self.assertEqual(rog.parse_paired_pids(self.PAIRED_REPLY), [0x1C25])

    def test_parse_paired_pids_multiple(self) -> None:
        """多台设备按 4 字节槽位依次解析，遇 0 结束。"""
        reply = hexbytes(
            "01 a0 00 01 00 25 1c 02 04 72 1a 01 00 00 00 00 00 00 00 00"
        )
        self.assertEqual(rog.parse_paired_pids(reply), [0x1C25, 0x1A72])

    def test_parse_paired_pids_empty(self) -> None:
        """全 0 的配对列表返回空（没有设备配对）。"""
        reply = hexbytes("01 a0 00 01 00 00 00 00 00 00 00 00 00 00 00 00")
        self.assertEqual(rog.parse_paired_pids(reply), [])

    def test_parse_paired_pids_rejects_short_and_no_answer(self) -> None:
        """长度不足或 FF AA 的回复不算配对列表。"""
        self.assertEqual(rog.parse_paired_pids(b""), [])
        self.assertEqual(rog.parse_paired_pids(hexbytes("01 a0 00")), [])
        self.assertEqual(rog.parse_paired_pids(hexbytes("01 ff aa 00 00 00 00 00 00")), [])

    def test_keyboard_62_percent_not_charging(self) -> None:
        """键盘 [6]=0x3E=62%，[9]=0 → 未充电。"""
        parsed = rog.parse_keyboard_reply(self.KEYBOARD_REPLY)
        self.assertIsNotNone(parsed)
        level, charging = parsed  # type: ignore[misc]
        self.assertEqual(level, 62)
        self.assertFalse(charging)

    def test_keyboard_charging_flag(self) -> None:
        """[9]=1 表示充电中。"""
        reply = bytearray(self.KEYBOARD_REPLY)
        reply[9] = 0x01
        parsed = rog.parse_keyboard_reply(bytes(reply))
        self.assertEqual(parsed, (62, True))

    def test_keyboard_no_answer(self) -> None:
        """FF AA 回复解析为 None（设备关机/休眠）。"""
        reply = hexbytes("02 ff aa 00 00 00 00 00 00 00 00 00 00 00 00 00")
        self.assertTrue(rog.is_no_answer(reply))
        self.assertIsNone(rog.parse_keyboard_reply(reply))

    def test_keyboard_rejects_wrong_echo(self) -> None:
        """回复的命令字不匹配（不是 12 01）时不算有效电量。"""
        reply = bytearray(self.KEYBOARD_REPLY)
        reply[2] = 0x07
        self.assertIsNone(rog.parse_keyboard_reply(reply))

    def test_keyboard_out_of_range(self) -> None:
        """电量字节 > 100 视为无效。"""
        reply = bytearray(self.KEYBOARD_REPLY)
        reply[6] = 0x7F  # 127
        self.assertIsNone(rog.parse_keyboard_reply(reply))

    def test_keyboard_falchion_offset(self) -> None:
        """Falchion 系列百分比在 [11]（[6] 是 0–10 档）。"""
        parsed = rog.parse_keyboard_reply(self.KEYBOARD_REPLY, battery_offset=11)
        self.assertEqual(parsed, (0x3E, False))  # 本夹具 [11]=0x3E=62

    def test_mouse_1_percent_scale(self) -> None:
        """新鼠标 [5] 直接是百分比，[10]!=0 表示充电。"""
        reply = hexbytes(
            "03 12 07 00 00 50 00 01 00 00 01 00 00 00 00 00"
        )
        self.assertEqual(rog.parse_mouse_reply(reply), (80, True))

    def test_mouse_gauge_scale_25(self) -> None:
        """0–4 档的老鼠标 ×25：raw=3 → 75%。"""
        reply = hexbytes("03 12 07 00 00 03 00 01 00 00 00 00 00 00 00 00")
        self.assertEqual(rog.parse_mouse_reply(reply, scale=25), (75, False))

    def test_mouse_zero_and_not_charging_is_invalid(self) -> None:
        """电量 0 且未充电 → 视为无有效读数（设备休眠）。"""
        reply = hexbytes("03 12 07 00 00 00 00 00 00 00 00 00 00 00 00 00")
        self.assertIsNone(rog.parse_mouse_reply(reply))

    def test_mouse_no_answer(self) -> None:
        """鼠标通道 FF AA → None。"""
        self.assertIsNone(rog.parse_mouse_reply(self.NO_ANSWER_REPLY))

    def test_build_request(self) -> None:
        """请求报文：第 0 字节 report id，长度对齐集合声明。"""
        packet = rog.build_request(0x02, 0x12, 0x01, 64)
        self.assertEqual(len(packet), 64)
        self.assertEqual(packet[:3], bytes((0x02, 0x12, 0x01)))
        self.assertEqual(packet[3:], bytes(61))

    def test_model_lookup(self) -> None:
        """型号表：OMNI 接收器上的 0x1C25 是 ROG 夜魔X。"""
        model = rog.find_model(0x1C25, "omni_receiver")
        self.assertIsNotNone(model)
        self.assertEqual(rog.model_name(model, 0x1C25), "ROG 夜魔X")
        self.assertEqual(model.protocol.battery_offset, 6)
        self.assertEqual(model.channel.col, "col02")

    def test_model_lookup_unknown(self) -> None:
        """未收录的 PID 给出带 PID 的兜底名字。"""
        self.assertIsNone(rog.find_model(0xFFFF, "omni_receiver"))
        self.assertIn("0xFFFF", rog.model_name(None, 0xFFFF))


# --------------------------------------------------------------------------
# 罗技
# --------------------------------------------------------------------------
class TestLogitechParsing(unittest.TestCase):
    """罗技 HID++ 2.0 解析（真机回复原样作为测试夹具）。"""

    # 真机回复：槽位 1 的短名 "G Pro"
    PAIRED_NAME_REPLY = hexbytes("11 ff 83 b5 40 06 47 20 50 72 6f 20 00 00 00 00")
    # 空槽位：HID++1.0 错误 0x8F，错误码 0x03
    EMPTY_SLOT_REPLY = hexbytes("10 ff 8f 83 b5 03 00")

    def test_paired_name(self) -> None:
        """短名：data[0] 回显子寄存器、data[1] 长度、data[2:] 名字。"""
        # 回复 [4:] = 40 06 47 20 50 72 6f 20
        self.assertEqual(logitech.parse_paired_name(self.PAIRED_NAME_REPLY[4:]), "G Pro")

    def test_paired_name_empty(self) -> None:
        """长度为 0 时返回 None（空槽位）。"""
        self.assertIsNone(logitech.parse_paired_name(hexbytes("40 00 00 00")))

    def test_paired_name_bolt(self) -> None:
        """Bolt 接收器多一个回显字节：data[2] 长度、data[3:] 名字。"""
        data = hexbytes("60 01 05 4d 58 20 4d")
        self.assertEqual(logitech.parse_paired_name(data, bolt=True), "MX M")

    def test_error_hidpp10(self) -> None:
        """0x8F 是接收器代答的 HID++1.0 错误。"""
        header = bytes((0xFF, 0x83, 0xB5))
        parsed = logitech.parse_error(self.EMPTY_SLOT_REPLY, header)
        self.assertEqual(parsed, ("hidpp10", 0x03))

    def test_error_hidpp20(self) -> None:
        """0xFF 是设备自己回的 HID++2.0 错误。"""
        reply = hexbytes("10 ff ff 00 05 09 00")
        self.assertEqual(logitech.parse_error(reply, bytes((0xFF, 0x00, 0x05))),
                         ("hidpp20", 0x09))

    def test_error_not_error(self) -> None:
        """正常回复不算错误。"""
        self.assertIsNone(logitech.parse_error(self.PAIRED_NAME_REPLY,
                                               bytes((0xFF, 0x83, 0xB5))))

    def test_feature_index(self) -> None:
        """找特性回复 data[0] = 特性索引，0 表示没有。"""
        self.assertEqual(logitech.parse_feature_index(hexbytes("06 00 00 00")), 6)
        self.assertIsNone(logitech.parse_feature_index(hexbytes("00 00 00 00")))
        self.assertIsNone(logitech.parse_feature_index(b""))

    def test_battery_voltage_3967mv(self) -> None:
        """0x1001：大端 mV，实测 3967 mV → 约 77%，未充电。"""
        # 真机 data = 0f 7a 00 ...  (0x0F7A = 3962)
        decoded = logitech.decode_battery(logitech.FEATURE_BATTERY_VOLTAGE,
                                          hexbytes("0f 7a 00 00 00 00"))
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded["voltage_mv"], 3962)
        self.assertEqual(decoded["level"], 76)
        self.assertTrue(decoded["estimated"])
        self.assertFalse(decoded["charging"])

    def test_battery_voltage_charging_bit(self) -> None:
        """0x1001 的 data[2] bit7 = 充电中。"""
        decoded = logitech.decode_battery(logitech.FEATURE_BATTERY_VOLTAGE,
                                          hexbytes("0f 7b 80 00 00 00"))
        assert decoded is not None
        self.assertEqual(decoded["voltage_mv"], 3963)
        self.assertTrue(decoded["charging"])
        self.assertEqual(decoded["level"], 76)

    def test_battery_voltage_out_of_range(self) -> None:
        """电压不在 2000–5000 mV 范围内视为无效。"""
        self.assertIsNone(logitech.decode_battery(logitech.FEATURE_BATTERY_VOLTAGE,
                                                  hexbytes("00 10 00")))
        self.assertIsNone(logitech.decode_battery(logitech.FEATURE_BATTERY_VOLTAGE,
                                                  hexbytes("ff ff 00")))

    def test_battery_unified_soc(self) -> None:
        """0x1004：soc 非 0 时直接是百分比，status 1–4 = 充电。"""
        decoded = logitech.decode_battery(logitech.FEATURE_UNIFIED_BATTERY,
                                          hexbytes("4b 00 01 00 00 00"))
        assert decoded is not None
        self.assertEqual(decoded["level"], 75)
        self.assertTrue(decoded["charging"])
        self.assertFalse(decoded["estimated"])

    def test_battery_unified_level_bits(self) -> None:
        """0x1004：soc=0 时退回粗档位 bit3=100 / bit2=50 / bit1=20 / bit0=5。"""
        cases = ((8, 100), (4, 50), (2, 20), (1, 5))
        for bits, expected in cases:
            decoded = logitech.decode_battery(
                logitech.FEATURE_UNIFIED_BATTERY, bytes((0, bits, 0, 0)))
            assert decoded is not None
            self.assertEqual(decoded["level"], expected, f"bits={bits}")
            self.assertTrue(decoded["estimated"])

    def test_battery_unified_all_zero_is_invalid(self) -> None:
        """soc=0 且档位全 0 → 无有效数据。"""
        self.assertIsNone(logitech.decode_battery(logitech.FEATURE_UNIFIED_BATTERY,
                                                  bytes(6)))

    def test_battery_status(self) -> None:
        """0x1000：data[0]=电量%，data[2]=状态。"""
        decoded = logitech.decode_battery(logitech.FEATURE_BATTERY_STATUS,
                                          hexbytes("3c 03 02 00 00 00"))
        assert decoded is not None
        self.assertEqual(decoded["level"], 60)
        self.assertTrue(decoded["charging"])
        self.assertIsNone(logitech.decode_battery(logitech.FEATURE_BATTERY_STATUS,
                                                  hexbytes("00 03 00")))

    def test_battery_adc(self) -> None:
        """0x1F20：data[2] bit0=有效、bit1=充电。"""
        decoded = logitech.decode_battery(logitech.FEATURE_ADC_MEASUREMENT,
                                          hexbytes("0f 7b 03"))
        assert decoded is not None
        self.assertEqual(decoded["voltage_mv"], 3963)
        self.assertTrue(decoded["charging"])
        self.assertIsNone(logitech.decode_battery(logitech.FEATURE_ADC_MEASUREMENT,
                                                  hexbytes("0f 7b 00")))

    def test_voltage_curve(self) -> None:
        """锂电池曲线端点与插值。"""
        self.assertEqual(logitech.voltage_percent(4200), 100)
        self.assertEqual(logitech.voltage_percent(4186), 100)
        self.assertEqual(logitech.voltage_percent(3811), 50)
        self.assertEqual(logitech.voltage_percent(3500), 0)
        self.assertEqual(logitech.voltage_percent(3400), 0)
        self.assertEqual(logitech.voltage_percent(3967), 77)
        # 3962 落在 3989(80%) 与 3922(70%) 之间
        self.assertEqual(logitech.voltage_percent(3962), 76)

    def test_status_from_charging(self) -> None:
        """充电布尔 → 契约 status。"""
        self.assertEqual(logitech.status_from_charging(True, 80), "charging")
        self.assertEqual(logitech.status_from_charging(True, 100), "full")
        self.assertEqual(logitech.status_from_charging(False, 80), "discharging")
        self.assertEqual(logitech.status_from_charging(False, None), "unknown")

    def test_build_short_request(self) -> None:
        """7 字节短请求：0x10 + 三字节头 + 参数。"""
        packet = logitech.build_short_request(bytes((0x01, 0x00, 0x0C)), bytes((0x10, 0x01)))
        self.assertEqual(len(packet), 7)
        self.assertEqual(packet, hexbytes("10 01 00 0c 10 01 00"))

    def test_short_name_to_display(self) -> None:
        """全名压成卡片显示名。"""
        self.assertEqual(logitech.short_name_to_display("G Pro Wireless Gaming Mouse"),
                         "罗技 G PRO Wireless")
        self.assertEqual(logitech.short_name_to_display("G Pro"), "罗技 G PRO")
        self.assertEqual(logitech.short_name_to_display(""), "罗技设备")
        # 已经带品牌前缀的不重复加
        self.assertEqual(logitech.short_name_to_display("Logitech MX Master 3S"),
                         "Logitech MX Master 3S")

    def test_receivers_table(self) -> None:
        """支持的接收器 PID 表。"""
        self.assertEqual(logitech.RECEIVERS[0xC539], "lightspeed")
        self.assertEqual(logitech.RECEIVERS[0xC548], "bolt")


# --------------------------------------------------------------------------
# 旧仓库协议（ATK/VXE + DARKRIM）
# --------------------------------------------------------------------------
class TestLegacyAtk(unittest.TestCase):
    """ATK / VXE 协议（CONTRACT §4.3，无真机，用固定字节串验证）。"""

    def test_build_request(self) -> None:
        """请求报文：report id + 载荷，补齐到 64 字节。"""
        protocol = legacy.ATK_PROTOCOLS[0]
        packet = legacy.build_atk_request(protocol)
        self.assertEqual(len(packet), 64)
        self.assertEqual(packet[0], 0x08)
        self.assertEqual(packet[1:8], bytes((0x7D, 0x72, 0x02, 0x00, 0x01, 0x07, 0x01)))

    def test_parse_reply_v2_ok(self) -> None:
        """协议 2：回复 [1]=0x72、[5]=0x07，电量在 [7]。"""
        protocol = legacy.ATK_PROTOCOLS[0]
        reply = bytearray(64)
        reply[1] = 0x72
        reply[5] = 0x07
        reply[7] = 0x37  # 55
        self.assertEqual(legacy.parse_atk_reply(protocol, bytes(reply)), (55, False))

    def test_parse_reply_v2_wrong_echo(self) -> None:
        """回复 [1] 不是 0x72 → 无效。"""
        protocol = legacy.ATK_PROTOCOLS[0]
        reply = bytearray(64)
        reply[1] = 0x00
        reply[5] = 0x07
        reply[7] = 55
        self.assertIsNone(legacy.parse_atk_reply(protocol, bytes(reply)))

    def test_parse_reply_v1_wired(self) -> None:
        """协议 1：电量在 [6]，[7]!=0 表示有线连接。"""
        protocol = legacy.ATK_PROTOCOLS[1]
        reply = bytearray(17)
        reply[6] = 0x50  # 80
        reply[7] = 0x01
        self.assertEqual(legacy.parse_atk_reply(protocol, bytes(reply)), (80, True))

    def test_parse_reply_short(self) -> None:
        """回复太短 → None。"""
        self.assertIsNone(legacy.parse_atk_reply(legacy.ATK_PROTOCOLS[1], b"\x08\x04"))

    def test_level_out_of_range(self) -> None:
        """电量 > 100 → 无效。"""
        protocol = legacy.ATK_PROTOCOLS[1]
        reply = bytearray(17)
        reply[6] = 0xFF
        self.assertIsNone(legacy.parse_atk_reply(protocol, bytes(reply)))


class TestLegacyDarkrim(unittest.TestCase):
    """DARKRIM TLV 协议（CONTRACT §4.3，无真机，用固定字节串验证）。"""

    def test_crc16_ccitt_known_value(self) -> None:
        """CRC16-CCITT-FALSE 的标准校验值（"123456789" → 0x29B1）。"""
        self.assertEqual(legacy.crc16_ccitt(b"123456789"), 0x29B1)

    def test_build_and_parse_roundtrip(self) -> None:
        """构造一帧再解析，CRC 必须通过、TLV 能还原。"""
        payload = bytes((legacy.TLV_BATTERY, 0x01, 0x46))  # TLV 0x01 电量=70
        frame = legacy.build_darkrim_frame(0, payload)
        self.assertEqual(len(frame), legacy.DARKRIM_REPORT_LEN)
        self.assertEqual(frame[0], 0x00)
        self.assertEqual(frame[1], 0xFE)
        parsed = legacy.parse_darkrim_frame(frame)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.crc_ok)
        self.assertEqual(parsed.seq, 0)
        self.assertEqual(parsed.payload, payload)
        self.assertEqual(parsed.tlvs(), [(legacy.TLV_BATTERY, bytes((0x46,)))])

    def test_crc_mismatch_detected(self) -> None:
        """载荷被改一个字节 → crc_ok=False。"""
        frame = bytearray(legacy.build_darkrim_frame(0, bytes((legacy.TLV_BATTERY, 1, 70))))
        frame[10 + 2] ^= 0xFF          # 改载荷
        parsed = legacy.parse_darkrim_frame(bytes(frame))
        assert parsed is not None
        self.assertFalse(parsed.crc_ok)

    def test_extract_battery_and_name(self) -> None:
        """电量 TLV(0x01) + 名字 TLV(0x02)。"""
        battery = bytes((legacy.TLV_BATTERY, 0x01, 0x46))
        name = bytes((legacy.TLV_NAME, 0x0F)) + b"DARKRIM SHADOW"
        frame = legacy.parse_darkrim_frame(
            legacy.build_darkrim_frame(0, battery + name))
        assert frame is not None
        level, device_name = legacy.extract_darkrim_battery([frame])
        self.assertEqual(level, 70)
        self.assertEqual(device_name, "DARKRIM SHADOW")

    def test_extract_ignores_bad_crc(self) -> None:
        """CRC 错误的帧被忽略。"""
        frame = bytearray(legacy.build_darkrim_frame(0, bytes((legacy.TLV_BATTERY, 1, 70))))
        frame[12] ^= 0xFF          # 改载荷里的电量字节（CRC 覆盖范围内）
        parsed = legacy.parse_darkrim_frame(bytes(frame))
        assert parsed is not None
        self.assertFalse(parsed.crc_ok)
        self.assertEqual(legacy.extract_darkrim_battery([parsed]), (None, ""))

    def test_invalid_frame(self) -> None:
        """帧头不对 / 太短 → None。"""
        self.assertIsNone(legacy.parse_darkrim_frame(b"\x00\x00"))
        self.assertIsNone(legacy.parse_darkrim_frame(bytes(65)))


# --------------------------------------------------------------------------
# 标准 HID / 兜底
# --------------------------------------------------------------------------
class TestGenericParsing(unittest.TestCase):
    """标准 HID 电量与兜底逻辑。"""

    def test_scale_level_logical_max(self) -> None:
        """按 LogicalMax 换算：0xFF 与 0x64 两种情况。"""
        self.assertEqual(generic.scale_level(50, 100), 50)
        self.assertEqual(generic.scale_level(127, 255), 50)
        self.assertEqual(generic.scale_level(10, 20), 50)
        self.assertEqual(generic.scale_level(0, 100), 0)
        self.assertEqual(generic.scale_level(200, 0), 78)   # 没有上限时按 0-255

    def test_describe_battery_usage(self) -> None:
        """只认标准电量 usage。"""
        self.assertIsNotNone(generic.describe_battery_usage(0x0006, 0x0020))
        self.assertIsNotNone(generic.describe_battery_usage(0x000D, 0x003B))
        self.assertIsNone(generic.describe_battery_usage(0x0001, 0x0006))


# --------------------------------------------------------------------------
# HID 路径解析
# --------------------------------------------------------------------------
class TestHidPath(unittest.TestCase):
    """HID 设备接口路径解析。"""

    ASUS_PATH = (r"\\?\hid#vid_0b05&pid_1ace&mi_02&col02#a&7aa98ac&0&0001"
                 r"#{4d1e55b2-f16f-11cf-88cb-001111000030}")

    def test_parse_path_info(self) -> None:
        """从路径解析 VID/PID/mi/col。"""
        vid, pid, mi, col = hidiface.parse_path_info(self.ASUS_PATH)
        self.assertEqual((vid, pid), (0x0B05, 0x1ACE))
        self.assertEqual(mi, 2)
        self.assertEqual(col, "col02")

    def test_parse_path_info_no_col(self) -> None:
        """没有 mi/col 的路径返回 None。"""
        vid, pid, mi, col = hidiface.parse_path_info(
            r"\\?\hid#hid_device_system_vhf#9&a168dd&0&0000#{guid}")
        self.assertEqual((vid, pid), (0, 0))
        self.assertIsNone(mi)
        self.assertIsNone(col)

    def test_instance_key_groups_collections(self) -> None:
        """同一 mi 的不同 col 归到同一个实例键。"""
        col1 = (r"\\?\hid#vid_046d&pid_c539&mi_02&col01#9&16c2a3a9&0&0000"
                r"#{4d1e55b2-f16f-11cf-88cb-001111000030}")
        col2 = (r"\\?\hid#vid_046d&pid_c539&mi_02&col02#9&16c2a3a9&0&0001"
                r"#{4d1e55b2-f16f-11cf-88cb-001111000030}")
        self.assertEqual(hidiface.instance_key(col1), hidiface.instance_key(col2))

    def test_instance_key_differs_for_two_receivers(self) -> None:
        """插两个相同接收器时实例键不同。"""
        first = (r"\\?\hid#vid_046d&pid_c539&mi_02&col01#9&16c2a3a9&0&0000"
                 r"#{4d1e55b2-f16f-11cf-88cb-001111000030}")
        second = (r"\\?\hid#vid_046d&pid_c539&mi_02&col01#9&aaaa1111&0&0000"
                  r"#{4d1e55b2-f16f-11cf-88cb-001111000030}")
        self.assertNotEqual(hidiface.instance_key(first), hidiface.instance_key(second))

    def test_physical_group_key(self) -> None:
        """物理分组键忽略 mi_/col_。"""
        key = hidiface.physical_group_key(self.ASUS_PATH)
        self.assertNotIn("mi_", key.lower())
        self.assertNotIn("col", key.lower())


# --------------------------------------------------------------------------
# reader
# --------------------------------------------------------------------------
class TestReaderContract(unittest.TestCase):
    """reader 的契约行为（不碰真实设备）。"""
    def test_demo_readings_shape(self) -> None:
        """demo 数据固定两台（UI 样式 B 双环），含充电中样例与电压估算样例。"""
        readings = reader.demo_readings()
        self.assertEqual(len(readings), 2)
        for reading in readings:
            self.assertIsInstance(reading, Reading)
            self.assertTrue(reading.device_id)
            self.assertTrue(reading.name)
            self.assertIn(reading.status,
                          {"discharging", "charging", "full", "unknown",
                           "unplugged", "error", "idle"})
        keyboard = readings[0]
        self.assertEqual(keyboard.level, 62)
        self.assertEqual(keyboard.status, "charging")     # 充电中样例（供动画联调）
        self.assertTrue(keyboard.charging)
        self.assertFalse(keyboard.inferred)
        self.assertFalse(keyboard.estimated)
        mouse = readings[1]
        self.assertEqual(mouse.voltage_mv, 3967)
        self.assertEqual(mouse.level, 77)
        self.assertTrue(mouse.estimated)                  # 电压估算带 ≈
        self.assertFalse(mouse.charging)
        self.assertEqual(mouse.status, "discharging")

    def test_demo_readings_fresh_timestamps(self) -> None:
        """demo 数据的时间戳是"刚刚"。"""
        for reading in reader.demo_readings():
            self.assertEqual(reading.age_text, "刚刚")

    def test_discover_returns_device_info(self) -> None:
        """discover() 返回 DeviceInfo 列表（允许空）。"""
        infos = reader.discover(force=True)
        self.assertIsInstance(infos, list)
        for info in infos:
            self.assertIsInstance(info, DeviceInfo)
            self.assertTrue(info.device_id)
            self.assertTrue(info.name)
            self.assertTrue(info.kind)

    def test_read_all_never_raises(self) -> None:
        """read_all() 永不抛异常，返回 Reading 列表。"""
        readings = reader.read_all()
        self.assertIsInstance(readings, list)
        for reading in readings:
            self.assertIsInstance(reading, Reading)
            self.assertIsInstance(reading.detail, str)
        self.assertGreaterEqual(reader.last_read_ms(), 0.0)


class _FakeBackend:
    """reader 测试用的假后端设备（形状与 RogDevice/LogitechDevice 一致）。"""

    def __init__(self, device_id: str, name: str, level: int = 50,
                 fail: bool = False) -> None:
        self.device_id = device_id
        self.name = name
        self.level = level
        self.fail = fail
        self.path = f"fake#{device_id}"

    def info(self) -> DeviceInfo:
        return DeviceInfo(device_id=self.device_id, name=self.name, kind="fake")

    def read(self) -> Reading:
        if self.fail:
            raise RuntimeError("模拟设备抽风")
        return Reading(device_id=self.device_id, name=self.name, level=self.level,
                       status="discharging")


class TestReaderCacheAndHotplug(unittest.TestCase):
    """发现缓存 / 热插拔重扫 / 异常隔离（用 mock 模拟硬件，不需要真机）。"""

    def _make_reader(self) -> "reader.DeviceReader":
        instance = reader.DeviceReader()
        instance.invalidate()
        return instance

    def test_cache_avoids_repeated_enumeration(self) -> None:
        """连续两轮 read_all 只做一次详细枚举（发现结果被缓存）。"""
        detail_calls = {"count": 0}

        def fake_enumerate(detail: bool = True):
            if detail:
                detail_calls["count"] += 1
            return []

        with mock.patch.object(reader.hidiface, "enumerate_interfaces", fake_enumerate), \
                mock.patch.object(reader.hidiface, "list_paths", lambda: []):
            instance = self._make_reader()
            instance.read_all()
            first = detail_calls["count"]
            instance.read_all()
            self.assertEqual(detail_calls["count"], first)
            self.assertEqual(first, 1)

    def test_path_change_triggers_rescan(self) -> None:
        """HID 路径集合变化时自动重新发现（热插拔）。"""
        detail_calls = {"count": 0}
        paths = {"value": []}

        def fake_enumerate(detail: bool = True):
            if detail:
                detail_calls["count"] += 1
            return []

        with mock.patch.object(reader.hidiface, "enumerate_interfaces", fake_enumerate), \
                mock.patch.object(reader.hidiface, "list_paths", lambda: list(paths["value"])):
            instance = self._make_reader()
            instance.read_all()
            self.assertEqual(detail_calls["count"], 1)
            # 模拟插上接收器（路径多了）——绕过 1 秒的轻量检查间隔
            paths["value"] = ["\\\\?\\hid#vid_0b05&pid_1ace&mi_02&col01#new"]
            instance._last_path_check = 0.0
            instance.read_all()
            self.assertEqual(detail_calls["count"], 2)

    def test_full_rescan_interval(self) -> None:
        """超过 FULL_RESCAN_SECONDS 强制重扫（配对表可能变化）。"""
        detail_calls = {"count": 0}

        def fake_enumerate(detail: bool = True):
            if detail:
                detail_calls["count"] += 1
            return []

        with mock.patch.object(reader.hidiface, "enumerate_interfaces", fake_enumerate), \
                mock.patch.object(reader.hidiface, "list_paths", lambda: []):
            instance = self._make_reader()
            instance.read_all()
            self.assertEqual(detail_calls["count"], 1)
            # 假装距离上次发现已经过了很久
            instance._discovered_at = time.monotonic() - (
                reader.FULL_RESCAN_SECONDS + 1.0)
            instance.read_all()
            self.assertEqual(detail_calls["count"], 2)

    def test_discovery_failure_does_not_raise(self) -> None:
        """枚举 HID 抛异常时 read_all 返回空列表而不是崩溃。"""
        def boom(detail: bool = True):
            raise OSError("模拟 SetupAPI 失败")

        with mock.patch.object(reader.hidiface, "enumerate_interfaces", boom), \
                mock.patch.object(reader.hidiface, "list_paths", lambda: []):
            instance = self._make_reader()
            readings = instance.read_all()
            self.assertEqual(readings, [])

    def test_backend_failure_isolated_per_device(self) -> None:
        """单台设备读取抛异常时只影响它自己，其余设备照常返回。"""
        good = _FakeBackend("fake:good", "好设备", level=66)
        bad = _FakeBackend("fake:bad", "坏设备", fail=True)
        with mock.patch.object(reader.rog, "discover", lambda *a, **k: [good, bad]), \
                mock.patch.object(reader.logitech, "find_receivers", lambda *a, **k: []), \
                mock.patch.object(reader.logitech, "discover", lambda *a, **k: []), \
                mock.patch.object(reader.hidiface, "enumerate_interfaces",
                                  lambda detail=True: []), \
                mock.patch.object(reader.hidiface, "list_paths", lambda: []):
            instance = self._make_reader()
            readings = instance.read_all()
        by_id = {item.device_id: item for item in readings}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id["fake:good"].level, 66)
        self.assertEqual(by_id["fake:bad"].status, "error")
        self.assertIn("模拟设备抽风", by_id["fake:bad"].detail)

    def test_invalidate_forces_discovery(self) -> None:
        """invalidate() 后下一次 read_all 重新发现。"""
        detail_calls = {"count": 0}

        def fake_enumerate(detail: bool = True):
            if detail:
                detail_calls["count"] += 1
            return []

        with mock.patch.object(reader.hidiface, "enumerate_interfaces", fake_enumerate), \
                mock.patch.object(reader.hidiface, "list_paths", lambda: []):
            instance = self._make_reader()
            instance.read_all()
            instance.invalidate()
            instance.read_all()
            self.assertEqual(detail_calls["count"], 2)


class TestChargingInference(unittest.TestCase):
    """协议没给充电标志时的推断（CONTRACT §4.5 A 方案）。"""

    def _reading(self, level: int, charging=None, status: str = "unknown") -> Reading:
        return Reading(device_id="fake:1", name="测试", level=level,
                       status=status, charging=charging)

    def test_rising_level_infers_charging(self) -> None:
        """电量上升 → 推断充电中，inferred=True。"""
        instance = reader.DeviceReader()
        instance._apply_inference(self._reading(60))
        result = instance._apply_inference(self._reading(62))
        self.assertTrue(result.charging)
        self.assertEqual(result.status, "charging")
        self.assertTrue(result.inferred)
        self.assertIn("推断", result.status_text)

    def test_falling_level_infers_discharging(self) -> None:
        """电量下降 → 推断未充电。"""
        instance = reader.DeviceReader()
        instance._apply_inference(self._reading(62))
        result = instance._apply_inference(self._reading(61))
        self.assertFalse(result.charging)
        self.assertEqual(result.status, "discharging")
        self.assertTrue(result.inferred)

    def test_unchanged_level_keeps_unknown(self) -> None:
        """电量不变 → 不推断（不伪造确定状态）。"""
        instance = reader.DeviceReader()
        instance._apply_inference(self._reading(62))
        result = instance._apply_inference(self._reading(62))
        self.assertIsNone(result.charging)
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.inferred)

    def test_protocol_charging_not_overridden(self) -> None:
        """协议给了充电标志就不再推断（inferred 保持 False）。"""
        instance = reader.DeviceReader()
        first = self._reading(62, charging=False, status="discharging")
        instance._apply_inference(first)
        # 电量即使上涨，也不能覆盖协议直读的 False
        result = instance._apply_inference(
            self._reading(63, charging=False, status="discharging"))
        self.assertFalse(result.charging)
        self.assertFalse(result.inferred)

    def test_no_level_no_inference(self) -> None:
        """读不到电量时不动状态。"""
        instance = reader.DeviceReader()
        result = instance._apply_inference(self._reading(None))  # type: ignore[arg-type]
        self.assertIsNone(result.level)
        self.assertIsNone(result.charging)

    def test_old_history_pruned(self) -> None:
        """窗口外的历史被清掉，不会拿很久以前的数据推断。"""
        instance = reader.DeviceReader()
        instance._history["fake:1"] = [(time.time() - 7200, 50)]
        result = instance._apply_inference(self._reading(62))
        self.assertIsNone(result.charging)
        # 历史里现在只应剩本次这一条
        self.assertEqual(len(instance._history["fake:1"]), 1)


class TestSimulatedSleepingDevices(unittest.TestCase):
    """模拟设备休眠/拔出时的 Reading（不打开真实 HID）。"""

    def test_rog_sleep_reply(self) -> None:
        """ROG 收到 FF AA → status=unplugged、level=None、detail 提示唤醒。"""
        device = rog.RogDevice(
            device_id="rog:0x1C25", name="ROG 夜魔X",
            model=rog.find_model(0x1C25, "omni_receiver"),
            path="\\\\?\\nonexistent",
            channel=rog.OMNI_KEYBOARD_CHANNEL, protocol=rog.KEYBOARD,
            connection="omni_receiver",
        )
        with mock.patch.object(rog, "query_channel",
                               lambda *a, **k: hexbytes("02 ff aa 00 00 00 00 00 00 00 00 00")):
            reading = device.read()
        self.assertEqual(reading.status, "unplugged")
        self.assertIsNone(reading.level)
        self.assertIn("唤醒", reading.detail)

    def test_rog_timeout(self) -> None:
        """ROG 完全没应答 → unplugged。"""
        device = rog.RogDevice(
            device_id="rog:0x1C25", name="ROG 夜魔X",
            model=rog.find_model(0x1C25, "omni_receiver"),
            path="\\\\?\\nonexistent",
            channel=rog.OMNI_KEYBOARD_CHANNEL, protocol=rog.KEYBOARD,
            connection="omni_receiver",
        )
        with mock.patch.object(rog, "query_channel", lambda *a, **k: None):
            reading = device.read()
        self.assertEqual(reading.status, "unplugged")
        self.assertIsNone(reading.level)

    def test_rog_unknown_model(self) -> None:
        """未适配型号：返回 unknown + detail 带真实 PID，不抛异常。"""
        device = rog.RogDevice(
            device_id="rog:0xFFFF", name="ROG 设备 0xFFFF", model=None,
            path="\\\\?\\nonexistent", channel=rog.OMNI_KEYBOARD_CHANNEL,
            protocol=rog.KEYBOARD, connection="omni_receiver", pid=0xFFFF,
        )
        reading = device.read()
        self.assertEqual(reading.status, "unknown")
        self.assertIn("0xFFFF", reading.detail)
        self.assertFalse(device.info().supported)

    def test_rog_exception_becomes_error_reading(self) -> None:
        """底层抛异常时返回 status=error 的 Reading，不向上抛。"""
        device = rog.RogDevice(
            device_id="rog:0x1C25", name="ROG 夜魔X",
            model=rog.find_model(0x1C25, "omni_receiver"),
            path="\\\\?\\nonexistent",
            channel=rog.OMNI_KEYBOARD_CHANNEL, protocol=rog.KEYBOARD,
            connection="omni_receiver",
        )
        with mock.patch.object(rog, "query_channel",
                               side_effect=OSError("模拟驱动错误")):
            reading = device.read()
        self.assertEqual(reading.status, "error")
        self.assertIn("模拟驱动错误", reading.detail)

    def test_logitech_unreachable(self) -> None:
        """罗技设备不可达 → unplugged，且不抛异常。"""
        device = logitech.LogitechDevice(
            device_id="logitech:C539#1", slot=1,
            short_path="\\\\?\\nonexistent", long_path="\\\\?\\nonexistent",
            connection="lightspeed", receiver_pid=0xC539, paired_name="G Pro",
        )
        with mock.patch.object(logitech, "Channel") as channel_cls:
            channel_cls.side_effect = OSError("模拟接收器被拔")
            reading = device.read()
        self.assertEqual(reading.status, "error")
        self.assertIn("模拟接收器被拔", reading.detail)

    def test_logitech_battery_decode_error_clears_cache(self) -> None:
        """设备拒绝读取时把特性索引缓存清掉，下次重新找。"""
        device = logitech.LogitechDevice(
            device_id="logitech:C539#1", slot=1,
            short_path="a", long_path="b", connection="lightspeed",
            receiver_pid=0xC539, paired_name="G Pro",
            battery_feature=(logitech.FEATURE_BATTERY_VOLTAGE, "0x1001 电池电压", 0),
            feature_index=6,
        )
        fake_channel = mock.MagicMock()
        fake_channel.call.return_value = logitech.Answer(
            kind="failed", code=9, error_kind="hidpp20")
        reading = device._read_with(fake_channel)
        self.assertEqual(reading.status, "unknown")
        self.assertIsNone(device.battery_feature)
        self.assertIsNone(device.feature_index)


# --------------------------------------------------------------------------
# 有线直连（假 HidInterfaceInfo + mock Channel，不需要真机）
# --------------------------------------------------------------------------
def fake_iface(vid: int, pid: int, instance: str, usage_page: int, usage: int,
               input_len: int, output_len: int, product: str = "",
               mi: int = 2, col: Optional[str] = "col01"
               ) -> hidiface.HidInterfaceInfo:
    """构造一个假的 HID 接口信息（instance 决定 hidiface.instance_key）。"""
    section = f"vid_{vid:04x}&pid_{pid:04x}&mi_{mi:02d}"
    if col:
        section += f"&{col}"
    path = (rf"\\?\hid#{section}#{instance}"
            r"#{4d1e55b2-f16f-11cf-88cb-001111000030}")
    return hidiface.HidInterfaceInfo(
        path=path, vid=vid, pid=pid, mi=mi, col=col,
        usage_page=usage_page, usage=usage,
        input_len=input_len, output_len=output_len, product=product)


def logitech_direct_ifaces(pid: int = 0xC088, instance: str = "9&8c58638&0",
                           product: str = "G Pro Wireless Gaming Mouse"
                           ) -> list[hidiface.HidInterfaceInfo]:
    """构造一台"有线罗技"的 short/long 两个集合（真机 C088 的形状）。"""
    return [
        fake_iface(0x046D, pid, instance, 0xFF00, 0x0001, 7, 7, product, col="col01"),
        fake_iface(0x046D, pid, instance, 0xFF00, 0x0002, 20, 20, product, col="col02"),
    ]


class _FakeHidppChannel:
    """假的 HID++ 通道：按请求返回预置回复（不开真 HID）。"""

    battery_data = hexbytes("0f 7a 00")      # 3962 mV → 76%
    device_name = "G Pro Wireless Gaming Mouse"
    battery_index = 6
    name_index = 7

    def __init__(self, *args, **kwargs) -> None:
        self.used = False

    def __enter__(self) -> "_FakeHidppChannel":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def close(self) -> None:
        return None

    def alive(self, *args, **kwargs) -> bool:
        return True

    def call(self, index: int, feature: int, function: int,
             params: bytes = b"", timeout_ms: int = 0) -> "logitech.Answer":
        if feature == logitech.ROOT and function == logitech.ROOT_GET_FEATURE:
            feature_id = ((params[0] << 8) | params[1]) if len(params) >= 2 else 0
            if feature_id == logitech.FEATURE_BATTERY_VOLTAGE:
                return logitech.Answer("reply", bytes((self.battery_index, 0, 2)))
            if feature_id == logitech.FEATURE_DEVICE_NAME:
                return logitech.Answer("reply", bytes((self.name_index, 0, 2)))
            return logitech.Answer("reply", bytes(3))
        if feature == self.battery_index:
            return logitech.Answer("reply", self.battery_data)
        if feature == self.name_index and function == 0:
            return logitech.Answer("reply", bytes((len(self.device_name),)))
        if feature == self.name_index and function == 1:
            return logitech.Answer("reply", self.device_name.encode())
        return logitech.UNREACHABLE


class TestLogitechDirect(unittest.TestCase):
    """有线直连罗技设备：短/长集合配对 + 0xFF 设备索引发现。"""

    def test_connection_text_wired(self) -> None:
        """连接方式文案：有线 USB。"""
        self.assertEqual(logitech.CONNECTION_TEXT["wired"], "有线 USB")
        self.assertEqual(logitech.DIRECT_DEVICE_INDEX, 0xFF)

    def test_pair_short_long_same_instance(self) -> None:
        """同一物理实例上的 0xFF00:0001 + 0xFF00:0002 配成一台直连设备。"""
        links = logitech.find_direct_devices(logitech_direct_ifaces())
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual(link.pid, 0xC088)
        self.assertEqual(link.product, "G Pro Wireless Gaming Mouse")
        self.assertIn("col01", link.short_path)
        self.assertIn("col02", link.long_path)

    def test_receiver_pid_is_not_direct(self) -> None:
        """接收器表里的 PID（如 C539）不当作有线直连设备。"""
        self.assertEqual(logitech.find_direct_devices(logitech_direct_ifaces(0xC539)), [])

    def test_short_only_is_not_direct(self) -> None:
        """只有短集合、缺长集合时不能配对（收不到回复）。"""
        self.assertEqual(
            logitech.find_direct_devices(logitech_direct_ifaces()[:1]), [])

    def test_unknown_receiver_product_is_not_direct(self) -> None:
        """PID 没收录、但产品名是 Receiver 的设备不能当有线设备。"""
        items = logitech_direct_ifaces(pid=0xC54A,
                                       product="USB Receiver")
        self.assertEqual(logitech.find_direct_devices(items), [])

    def test_two_instances_same_pid(self) -> None:
        """插两个同型号有线设备：物理实例不同 → 两台，不互相串。"""
        items = (logitech_direct_ifaces(instance="9&aaaa1111&0")
                 + logitech_direct_ifaces(instance="9&bbbb2222&0"))
        links = logitech.find_direct_devices(items)
        self.assertEqual(len(links), 2)
        self.assertNotEqual(links[0].instance, links[1].instance)

    def test_discover_direct_builds_wired_device(self) -> None:
        """有 HID++ 应答时构造 LogitechDevice：slot=0xFF / wired / 产品名兜底。"""
        with mock.patch.object(logitech, "Channel", _FakeHidppChannel):
            devices = logitech.discover_direct(logitech_direct_ifaces())
        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertEqual(device.device_id, "logitech:C088#wired")
        self.assertEqual(device.slot, 0xFF)
        self.assertEqual(device.connection, "wired")
        self.assertEqual(device.connection_text, "有线 USB")
        self.assertTrue(device.is_wired)
        self.assertEqual(device.full_name, "G Pro Wireless Gaming Mouse")
        self.assertEqual(device.name, "罗技 G PRO Wireless")
        info = device.info()
        self.assertEqual(info.connection, "wired")
        self.assertEqual(info.vid_pid, "046D:C088")
        self.assertTrue(info.supported)

    def test_discover_direct_read_goes_through_0xff(self) -> None:
        """直连设备读数复用现有特性探测/解码（电压 3962mV → ≈76%）。"""
        with mock.patch.object(logitech, "Channel", _FakeHidppChannel):
            device = logitech.discover_direct(logitech_direct_ifaces())[0]
            reading = device.read()
        self.assertEqual(reading.level, 76)
        self.assertTrue(reading.estimated)
        self.assertIn("有线 USB", reading.detail)
        self.assertNotIn("槽位", reading.detail)

    def test_discover_direct_open_failure_is_silent(self) -> None:
        """通道打不开（被拔/被独占）时静默跳过，不抛异常。"""
        with mock.patch.object(logitech, "Channel", side_effect=OSError("模拟被拔")):
            self.assertEqual(logitech.discover_direct(logitech_direct_ifaces()), [])

    def test_discover_direct_no_hidpp_answer_is_skipped(self) -> None:
        """有 0xFF00 集合但不讲 HID++（如某些耳机）→ 不误报成设备。"""

        class _DeadChannel(_FakeHidppChannel):
            def alive(self, *args, **kwargs) -> bool:
                return False

        with mock.patch.object(logitech, "Channel", _DeadChannel):
            self.assertEqual(logitech.discover_direct(logitech_direct_ifaces()), [])

    def test_discover_direct_no_battery_feature_is_skipped(self) -> None:
        """讲 HID++ 但没有电量特性（如纯有线键盘）→ 不生成空卡片。"""

        class _NoBatteryChannel(_FakeHidppChannel):
            def call(self, index, feature, function, params=b"", timeout_ms=0):
                if feature == logitech.ROOT and function == logitech.ROOT_GET_FEATURE:
                    return logitech.Answer("reply", bytes(3))   # 特性索引 0 = 没有
                return logitech.Answer("reply", bytes(3))

        with mock.patch.object(logitech, "Channel", _NoBatteryChannel):
            self.assertEqual(logitech.discover_direct(logitech_direct_ifaces()), [])

    def test_discover_direct_default_uses_enumerate(self) -> None:
        """不传 interfaces 时自己枚举（这里 mock 掉枚举，验证走同一条路径）。"""
        with mock.patch.object(logitech.hidiface, "enumerate_interfaces",
                               lambda detail=True: logitech_direct_ifaces()), \
                mock.patch.object(logitech, "Channel", _FakeHidppChannel):
            devices = logitech.discover_direct()
        self.assertEqual([item.device_id for item in devices], ["logitech:C088#wired"])


class TestRogDirect(unittest.TestCase):
    """ROG 有线直连尽力探测（0B05 厂商页 + OMNI 12 01/12 07）。"""

    KEYBOARD_REPLY = hexbytes(
        "02 12 01 00 00 00 3e 03 01 00 14 3e 3c 0f 00 00 00 00 00 00")

    @staticmethod
    def rog_ifaces(pid: int = 0x1A99, product: str = "ROG AZOTH X",
                   instance: str = "a&123456&0") -> list[hidiface.HidInterfaceInfo]:
        """一台未知 PID 的 ROG 键盘：标准键盘集合 + 厂商页集合。"""
        return [
            fake_iface(0x0B05, pid, instance, 0x0001, 0x0006, 9, 2, product,
                       mi=0, col="col01"),
            fake_iface(0x0B05, pid, instance, 0xFF00, 0x0001, 65, 65, product,
                       mi=1, col=None),
        ]

    def test_name_hint_filters_non_peripherals(self) -> None:
        """产品名像 ROG 外设才试；AURA LED 控制器不碰（不误报/不拖慢）。"""
        aura = [fake_iface(0x0B05, 0x19AF, "a&1&0", 0xFF72, 0x00A1, 65, 65,
                           "AURA LED Controller", mi=2, col=None)]
        rog_keyboard = self.rog_ifaces()
        self.assertTrue(rog._looks_like_rog_peripheral(rog_keyboard))
        self.assertFalse(rog._looks_like_rog_peripheral(aura))

    def test_direct_candidates_prefer_vendor_page(self) -> None:
        """候选集合只留厂商页、能读写的那个（标准键盘集合被排除）。"""
        candidates = rog._direct_candidates(self.rog_ifaces())
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].usage_page, 0xFF00)

    def test_probe_direct_device_keyboard(self) -> None:
        """键盘 12 01 回复能解析出电量才算命中。"""
        items = self.rog_ifaces()
        item = rog._direct_candidates(items)[0]
        with mock.patch.object(rog, "query_channel",
                               lambda *a, **k: self.KEYBOARD_REPLY):
            found = rog.probe_direct_device(item.path, item)
        self.assertIsNotNone(found)
        protocol, channel = found  # type: ignore[misc]
        self.assertEqual(protocol.kind, "keyboard")
        self.assertEqual(channel.report_id, 0x00)

    def test_probe_direct_device_rejects_garbage(self) -> None:
        """回复不是 12 01 / 12 07 电量格式 → 不认。"""
        item = rog._direct_candidates(self.rog_ifaces())[0]
        garbage = hexbytes("02 55 66 00 00 00 00 00 00 00 00 00 00 00 00 00")
        with mock.patch.object(rog, "query_channel", lambda *a, **k: garbage):
            self.assertIsNone(rog.probe_direct_device(item.path, item))

    def test_probe_direct_device_timeout(self) -> None:
        """完全没应答 → None（静默跳过）。"""
        item = rog._direct_candidates(self.rog_ifaces())[0]
        with mock.patch.object(rog, "query_channel", lambda *a, **k: None):
            self.assertIsNone(rog.probe_direct_device(item.path, item))

    def test_discover_direct_creates_wired_device(self) -> None:
        """探测命中后构造 wired 设备，device_id 形如 rog:0x1A99#wired。"""
        with mock.patch.object(
                rog, "probe_direct_device",
                return_value=(rog.KEYBOARD, rog.KEYBOARD_VENDOR)):
            devices = rog.discover_direct(self.rog_ifaces())
        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertEqual(device.device_id, "rog:0x1A99#wired")
        self.assertEqual(device.connection, "wired")
        self.assertEqual(device.name, "ROG AZOTH X")
        self.assertEqual(device.info().vid_pid, "0B05:1A99")
        self.assertTrue(device.info().supported)
        with mock.patch.object(rog, "query_channel",
                               lambda *a, **k: self.KEYBOARD_REPLY):
            reading = device.read()
        self.assertEqual(reading.level, 62)
        self.assertEqual(reading.status, "discharging")
        self.assertIn("12 01", reading.detail)

    def test_discover_direct_failure_is_silent(self) -> None:
        """探测失败不产生设备、不抛异常。"""
        with mock.patch.object(rog, "probe_direct_device", return_value=None):
            self.assertEqual(rog.discover_direct(self.rog_ifaces()), [])

    def test_discover_direct_skips_aura(self) -> None:
        """AURA LED 控制器不会被探测。"""
        aura = [fake_iface(0x0B05, 0x19AF, "a&1&0", 0xFF72, 0x00A1, 65, 65,
                           "AURA LED Controller", mi=2, col=None)]
        with mock.patch.object(rog, "probe_direct_device") as probe:
            self.assertEqual(rog.discover_direct(aura), [])
            probe.assert_not_called()

    def test_discover_direct_skips_known_model_and_claimed(self) -> None:
        """型号表里已知的 PID、以及已认领的实例都不重复探测。"""
        known = self.rog_ifaces(pid=0x1C25, product="ROG Azoth X",
                                instance="a&known&0")
        with mock.patch.object(rog, "probe_direct_device") as probe:
            self.assertEqual(rog.discover_direct(known), [])
            probe.assert_not_called()
        claimed = [item.path for item in self.rog_ifaces()]
        with mock.patch.object(rog, "probe_direct_device") as probe:
            self.assertEqual(rog.discover_direct(self.rog_ifaces(),
                                                 skip_paths=claimed), [])
            probe.assert_not_called()


class TestWiredDedupe(unittest.TestCase):
    """reader：有线直连设备与接收器槽位设备去重。"""

    class _Obj:
        def __init__(self, is_wired: bool, full_name: str = "",
                     paired_name: str = "", battery_feature=None) -> None:
            self.is_wired = is_wired
            self.full_name = full_name
            self.paired_name = paired_name
            self.battery_feature = battery_feature

    def _entry(self, device_id: str, obj, connection: str) -> "reader._Entry":
        return reader._Entry(device_id, "logitech", obj,
                             DeviceInfo(device_id=device_id, name=device_id,
                                        kind="logitech_hidpp", connection=connection))

    def test_receiver_slot_removed_when_wired_same_device(self) -> None:
        """接收器短名 "G Pro" 是有线全名的前缀 → 同一台，去掉接收器那条。"""
        receiver = self._entry("logitech:C539#1",
                               self._Obj(False, paired_name="G Pro"), "lightspeed")
        wired = self._entry("logitech:C088#wired",
                            self._Obj(True, full_name="G Pro Wireless Gaming Mouse",
                                      paired_name="G Pro Wireless Gaming Mouse",
                                      battery_feature=(0x1001, "0x1001", 0)),
                            "wired")
        kept = reader._dedupe_wired([receiver, wired])
        self.assertEqual([entry.device_id for entry in kept],
                         ["logitech:C088#wired"])

    def test_different_devices_are_kept(self) -> None:
        """名字对不上就不去重（宁可多一张，不误删）。"""
        receiver = self._entry("logitech:C539#2",
                               self._Obj(False, paired_name="MX Master"), "lightspeed")
        wired = self._entry("logitech:C088#wired",
                            self._Obj(True, full_name="G Pro Wireless Gaming Mouse",
                                      battery_feature=(0x1001, "0x1001", 0)),
                            "wired")
        kept = reader._dedupe_wired([receiver, wired])
        self.assertEqual(len(kept), 2)

    def test_wired_without_battery_feature_does_not_dedupe(self) -> None:
        """有线设备没探到电量特性（可能刚拔）时不去重接收器里的设备。"""
        receiver = self._entry("logitech:C539#1",
                               self._Obj(False, paired_name="G Pro"), "lightspeed")
        wired = self._entry("logitech:C088#wired",
                            self._Obj(True, full_name="G Pro Wireless Gaming Mouse"),
                            "wired")
        kept = reader._dedupe_wired([receiver, wired])
        self.assertEqual(len(kept), 2)


class TestReaderDirectIntegration(unittest.TestCase):
    """reader._rediscover 接入直连发现（mock 掉真实硬件）。"""

    def test_read_all_includes_direct_devices(self) -> None:
        """接收器发现之后会分别调用 logitech/rog 的 discover_direct。"""
        wired = _FakeBackend("logitech:C088#wired", "罗技 G PRO Wireless", level=76)
        rog_wired = _FakeBackend("rog:0x1A99#wired", "ROG AZOTH X", level=62)
        with mock.patch.object(reader.rog, "discover", lambda *a, **k: []), \
                mock.patch.object(reader.rog, "discover_direct",
                                  lambda *a, **k: [rog_wired]), \
                mock.patch.object(reader.logitech, "find_receivers",
                                  lambda *a, **k: []), \
                mock.patch.object(reader.logitech, "discover", lambda *a, **k: []), \
                mock.patch.object(reader.logitech, "discover_direct",
                                  lambda *a, **k: [wired]), \
                mock.patch.object(reader.hidiface, "enumerate_interfaces",
                                  lambda detail=True: []), \
                mock.patch.object(reader.hidiface, "list_paths", lambda: []):
            instance = reader.DeviceReader()
            readings = instance.read_all()
        by_id = {item.device_id: item for item in readings}
        self.assertIn("logitech:C088#wired", by_id)
        self.assertIn("rog:0x1A99#wired", by_id)
        self.assertEqual(by_id["logitech:C088#wired"].level, 76)
        self.assertEqual(by_id["rog:0x1A99#wired"].level, 62)

    def test_direct_discovery_failure_isolated(self) -> None:
        """直连发现抛异常时不影响接收器设备。"""
        receiver_device = _FakeBackend("logitech:C539#1", "罗技 G PRO", level=77)

        def boom(*args, **kwargs):
            raise OSError("模拟直连探测崩溃")

        with mock.patch.object(reader.rog, "discover", lambda *a, **k: []), \
                mock.patch.object(reader.rog, "discover_direct", boom), \
                mock.patch.object(reader.logitech, "find_receivers",
                                  lambda *a, **k: []), \
                mock.patch.object(reader.logitech, "discover",
                                  lambda *a, **k: [receiver_device]), \
                mock.patch.object(reader.logitech, "discover_direct", boom), \
                mock.patch.object(reader.hidiface, "enumerate_interfaces",
                                  lambda detail=True: []), \
                mock.patch.object(reader.hidiface, "list_paths", lambda: []):
            instance = reader.DeviceReader()
            readings = instance.read_all()
        self.assertEqual([item.device_id for item in readings],
                         ["logitech:C539#1"])


# --------------------------------------------------------------------------
# 结果模型
# --------------------------------------------------------------------------
class TestReadingModel(unittest.TestCase):
    """Reading 的展示辅助（设备层会用到 estimated/charging）。"""

    def test_level_text_estimated(self) -> None:
        """估算百分比带 ≈ 前缀。"""
        self.assertEqual(Reading(device_id="x", name="y", level=77,
                                 estimated=True).level_text, "≈77%")
        self.assertEqual(Reading(device_id="x", name="y", level=62).level_text, "62%")

    def test_status_text_inferred(self) -> None:
        """推断状态带"（推断）"。"""
        reading = Reading(device_id="x", name="y", status="charging", inferred=True)
        self.assertIn("推断", reading.status_text)
        reading.inferred = False
        self.assertEqual(reading.status_text, "充电中")


if __name__ == "__main__":
    unittest.main(verbosity=2)
