import logging
import os

from flipper.utils.programmer import Programmer
from flipper.utils.openocd import OpenOCD
from flipper.utils.stm32wb55 import STM32WB55
from flipper.assets.obdata import OptionBytesData


class OpenOCDProgrammer(Programmer):
    def __init__(
        self,
        interface: str = "interface/cmsis-dap.cfg",
        port_base: int | None = None,
        serial: str | None = None,
    ):
        super().__init__()

        config = {}

        config["interface"] = interface
        config["target"] = "target/stm32wbx.cfg"

        if not serial is None:
            if interface == "interface/cmsis-dap.cfg":
                config["serial"] = f"cmsis_dap_serial {serial}"
            elif "stlink" in interface:
                config["serial"] = f"stlink_serial {serial}"

        if not port_base is None:
            config["port_base"] = port_base

        self.openocd = OpenOCD(config)
        self.logger = logging.getLogger()

    def reset(self, mode: Programmer.RunMode = Programmer.RunMode.Run) -> bool:
        stm32 = STM32WB55()
        if mode == Programmer.RunMode.Run:
            stm32.reset(self.openocd, stm32.RunMode.Run)
        elif mode == Programmer.RunMode.Stop:
            stm32.reset(self.openocd, stm32.RunMode.Init)
        else:
            raise Exception("Unknown mode")

        return True

    def flash(self, address: int, file_path: str, verify: bool = True) -> bool:
        if not os.path.exists(file_path):
            raise Exception(f"File {file_path} not found")

        self.openocd.start()
        self.openocd.send_tcl(f"init")
        self.openocd.send_tcl(
            f"program {file_path} 0x{address:08x}{' verify' if verify else ''} reset exit"
        )
        self.openocd.stop()

        return True

    def _ob_print_diff_table(self, ob_reference: bytes, ob_read: bytes, print_fn):
        print_fn(
            f'{"Reference": <20} {"Device": <20} {"Diff Reference": <20} {"Diff Device": <20}'
        )

        # Split into 8 byte, word + word
        for i in range(0, len(ob_reference), 8):
            ref = ob_reference[i : i + 8]
            read = ob_read[i : i + 8]

            diff_str1 = ""
            diff_str2 = ""
            for j in range(0, len(ref.hex()), 2):
                byte_str_1 = ref.hex()[j : j + 2]
                byte_str_2 = read.hex()[j : j + 2]

                if byte_str_1 == byte_str_2:
                    diff_str1 += "__"
                    diff_str2 += "__"
                else:
                    diff_str1 += byte_str_1
                    diff_str2 += byte_str_2

            print_fn(
                f"{ref.hex(): <20} {read.hex(): <20} {diff_str1: <20} {diff_str2: <20}"
            )

    def option_bytes_validate(self, file_path: str) -> bool:
        # Registers
        stm32 = STM32WB55()

        # OpenOCD
        self.openocd.start()
        stm32.reset(self.openocd, stm32.RunMode.Init)

        # Generate Option Bytes data
        ob_data = OptionBytesData(file_path)
        ob_values = ob_data.gen_values().export()
        ob_reference = ob_values.reference
        ob_compare_mask = ob_values.compare_mask
        ob_length = len(ob_reference)
        ob_words = int(ob_length / 4)

        # Read Option Bytes
        ob_read = bytes()
        for i in range(ob_words):
            addr = stm32.OPTION_BYTE_BASE + i * 4
            value = self.openocd.read_32(addr)
            ob_read += value.to_bytes(4, "little")

        # Compare Option Bytes with reference by mask
        ob_compare = bytes()
        for i in range(ob_length):
            ob_compare += bytes([ob_read[i] & ob_compare_mask[i]])

        # Compare Option Bytes
        return_code = False

        if ob_reference == ob_compare:
            self.logger.info("Option Bytes are valid")
        else:
            self.logger.error("Option Bytes are invalid")
            self._ob_print_diff_table(ob_reference, ob_compare, self.logger.error)
            return_code = True

        # Stop OpenOCD
        stm32.reset(self.openocd, stm32.RunMode.Run)
        self.openocd.stop()

        return return_code

    def _unpack_u32(self, data: bytes, offset: int):
        return int.from_bytes(data[offset : offset + 4], "little")

    def option_bytes_set(self, file_path: str) -> bool:
        self.logger.info(f"Setting Option Bytes")

        # Registers
        stm32 = STM32WB55()

        # OpenOCD
        self.openocd.start()
        stm32.reset(self.openocd, stm32.RunMode.Init)

        # Generate Option Bytes data
        ob_data = OptionBytesData(file_path)
        ob_values = ob_data.gen_values().export()
        ob_reference_bytes = ob_values.reference
        ob_compare_mask_bytes = ob_values.compare_mask
        ob_write_mask_bytes = ob_values.write_mask
        ob_length = len(ob_reference_bytes)
        ob_dwords = int(ob_length / 8)

        # Clear flash errors
        stm32.clear_flash_errors(self.openocd)

        # Unlock Flash and Option Bytes
        stm32.flash_unlock(self.openocd)
        stm32.option_bytes_unlock(self.openocd)

        ob_need_to_apply = False

        for i in range(ob_dwords):
            device_addr = stm32.OPTION_BYTE_BASE + i * 8
            device_value = self.openocd.read_32(device_addr)
            ob_write_mask = self._unpack_u32(ob_write_mask_bytes, i * 8)
            ob_compare_mask = self._unpack_u32(ob_compare_mask_bytes, i * 8)
            ob_value_ref = self._unpack_u32(ob_reference_bytes, i * 8)
            ob_value_masked = device_value & ob_compare_mask

            need_patch = ((ob_value_masked ^ ob_value_ref) & ob_write_mask) != 0
            if need_patch:
                ob_need_to_apply = True

                self.logger.info(
                    f"Need to patch: {device_addr:08X}: {ob_value_masked:08X} != {ob_value_ref:08X}, REG[{i}]"
                )

                # Check if this option byte (dword) is mapped to a register
                device_reg_addr = stm32.option_bytes_id_to_address(i)

                # Construct new value for the OB register
                ob_value = device_value & (~ob_write_mask)
                ob_value |= ob_value_ref & ob_write_mask

                self.logger.info(f"Writing {ob_value:08X} to {device_reg_addr:08X}")
                self.openocd.write_32(device_reg_addr, ob_value)

        if ob_need_to_apply:
            stm32.option_bytes_apply(self.openocd)
        else:
            self.logger.info(f"Option Bytes are already correct")

        # Load Option Bytes
        # That will reset and also lock the Option Bytes and the Flash
        stm32.option_bytes_load(self.openocd)

        # Stop OpenOCD
        stm32.reset(self.openocd, stm32.RunMode.Run)
        self.openocd.stop()

        return True

    def otp_write(self, address: int, data: bytes) -> bool:
        oocd = self.openocd
        oocd.start()

        # Registers
        stm32 = STM32WB55()

        self.reset(self.RunMode.Stop)
        stm32.clear_flash_errors(oocd)

        # Read OTP memory
        self.logger.info(oocd.send_tcl(f"mdw {address} 2").strip())
        self.logger.info(oocd.send_tcl(f"mdw {address + 8} 2").strip())

        # Write OTP memory
        stm32.write_flash(oocd, address, 0x12345678, 0x9ABCDEF1)
        stm32.write_flash(oocd, address + 8, 0x9ABCDEF1, 0x12345678)

        # Read OTP memory again
        self.logger.info(oocd.send_tcl(f"mdw {address} 2").strip())
        self.logger.info(oocd.send_tcl(f"mdw {address + 8} 2").strip())

        # Stop OpenOCD
        stm32.reset(oocd, stm32.RunMode.Run)
        oocd.stop()
        return True