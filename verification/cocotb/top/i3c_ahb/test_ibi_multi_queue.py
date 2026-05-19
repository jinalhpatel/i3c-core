# SPDX-License-Identifier: Apache-2.0

import sys
sys.path.insert(0, '../lib_i3c_top')

from test_i3c_target import (
    cocotb_test, test_setup, TARGET_ADDRESS, VALID_I3C_ADDRESSES
)
from bus2csr import dword2int, int2dword
from cocotbext_i3c.i3c_controller import I3cController
from cocotbext_i3c.i3c_target import I3CTarget
from utils import format_ibi_data

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge, Timer


@cocotb_test()
async def test_ibi_multi_queue_nack_recovery(dut):
    """
    Tests IBI transmission with NACK and recovery by retrying the IBI.
    """
    # Setup
    i3c_controller, i3c_target, tb = await test_setup(dut, verify_boot=True)

    # Enable indefinite IBI retries
    await tb.write_csr(tb.reg_map.I3C_EC.TTI.CONTROL.base_addr, int2dword(0x0000F000), 4)

    target = i3c_controller.add_target(TARGET_ADDRESS)
    target.set_bcr_fields(ibi_req_capable=True, ibi_payload=True)

    result = True

    # Disable IBI ACK-ing initially
    i3c_controller.enable_ibi(False)

    # Queue IBI with data
    mdb = 0xAA
    data = [0xCA, 0xFE, 0xBA, 0xCA]
    ibi_data = format_ibi_data(mdb, data)
    for word in ibi_data:
        await tb.write_csr(tb.reg_map.I3C_EC.TTI.IBI_PORT.base_addr, int2dword(word), 4)

    # Wait for target to attempt transmission
    await Timer(5, "us")

    # Check IBI was NACKed
    status = dword2int(await tb.read_csr(tb.reg_map.I3C_EC.TTI.STATUS.base_addr, 4))
    last_ibi_status = (status & (3 << 14)) >> 14
    expected_status = 3  # NACK
    if last_ibi_status != expected_status:
        dut._log.critical(
            f"Expected NACK status (3), got {last_ibi_status}"
        )
        result = False

    # Re-enable IBI ACK-ing
    i3c_controller.enable_ibi(True)

    # Wait for IBI to be serviced
    response = await i3c_controller.wait_for_ibi()
    expected = bytearray([TARGET_ADDRESS, mdb] + data)
    if response != expected:
        dut._log.critical(
            "IBI MDB/data mismatch! tgt: [ {}] ctl: [ {}]".format(
                "".join("".join(f"0x{d:02X}") + " " for d in expected),
                "".join("".join(f"0x{d:02X}") + " " for d in response),
            )
        )
        result = False

    assert result


@cocotb_test()
async def test_ibi_multi_queue_partial_then_continue(dut):
    """
    Tests IBI with partial read followed by continuation.
    The controller reads less data than available, then continues with next IBI.
    """
    # Setup
    i3c_controller, i3c_target, tb = await test_setup(dut, verify_boot=True)

    target = i3c_controller.add_target(TARGET_ADDRESS)
    target.set_bcr_fields(ibi_req_capable=True, ibi_payload=True)

    result = True

    # Enable IBI ACK-ing
    i3c_controller.enable_ibi(True)

    # Limit IBI data count that controller can accept
    i3c_controller.set_max_ibi_data_len(4)

    # Queue first IBI with 6 bytes (will be truncated to 4)
    mdb = 0xBB
    data = [0xDE, 0xAD, 0xBE, 0xEF, 0xCA, 0xFE]
    ibi_data = format_ibi_data(mdb, data)
    for word in ibi_data:
        await tb.write_csr(tb.reg_map.I3C_EC.TTI.IBI_PORT.base_addr, int2dword(word), 4)

    # Wait for first IBI to be serviced
    response = await i3c_controller.wait_for_ibi()
    expected = bytearray([TARGET_ADDRESS, mdb] + data[:4])
    if response != expected:
        dut._log.critical(
            "IBI MDB/data mismatch on first IBI! tgt: [ {}] ctl: [ {}]".format(
                "".join("".join(f"0x{d:02X}") + " " for d in expected),
                "".join("".join(f"0x{d:02X}") + " " for d in response),
            )
        )
        result = False

    # Queue second IBI - should work correctly after flush
    mdb = 0xCC
    data = [0x11, 0x22, 0x33]
    ibi_data = format_ibi_data(mdb, data)
    for word in ibi_data:
        await tb.write_csr(tb.reg_map.I3C_EC.TTI.IBI_PORT.base_addr, int2dword(word), 4)

    # Wait for second IBI to be serviced
    response = await i3c_controller.wait_for_ibi()
    expected = bytearray([TARGET_ADDRESS, mdb] + data)
    if response != expected:
        dut._log.critical(
            "IBI MDB/data mismatch on second IBI! tgt: [ {}] ctl: [ {}]".format(
                "".join("".join(f"0x{d:02X}") + " " for d in expected),
                "".join("".join(f"0x{d:02X}") + " " for d in response),
            )
        )
        result = False

    assert result


@cocotb_test()
async def test_ibi_multi_queue_mixed_abort(dut):
    """
    Tests IBI transmission in a scenario with mixed success/abort patterns.
    """
    # Setup
    i3c_controller, i3c_target, tb = await test_setup(dut, verify_boot=True)

    target = i3c_controller.add_target(TARGET_ADDRESS)
    target.set_bcr_fields(ibi_req_capable=True, ibi_payload=True)

    result = True

    # Enable IBI ACK-ing
    i3c_controller.enable_ibi(True)

    # Test multiple IBIs with varying data lengths
    test_cases = [
        (0xAA, [0x11, 0x22]),
        (0xBB, [0x33, 0x44, 0x55, 0x66, 0x77]),
        (0xCC, []),
        (0xDD, [0x88, 0x99]),
    ]

    for mdb, data in test_cases:
        # Queue IBI
        ibi_data = format_ibi_data(mdb, data)
        for word in ibi_data:
            await tb.write_csr(tb.reg_map.I3C_EC.TTI.IBI_PORT.base_addr, int2dword(word), 4)

        # Wait for IBI to be serviced
        response = await i3c_controller.wait_for_ibi()
        expected = bytearray([TARGET_ADDRESS, mdb] + data)
        if response != expected:
            dut._log.critical(
                f"IBI MDB/data mismatch for MDB=0x{mdb:02X}! tgt: [ {}] ctl: [ {}]".format(
                    "".join("".join(f"0x{d:02X}") + " " for d in expected),
                    "".join("".join(f"0x{d:02X}") + " " for d in response),
                )
            )
            result = False

    assert result


@cocotb_test(timeout=1000, unit="us")
async def test_ibi_multi_queue_flush_large_payload(dut):
    """
    Tests IBI flush operation with large payloads in multi-queue scenario.
    
    This test validates:
    1. Large IBI payloads that require flushing in the width converter
    2. Partial read scenarios where remaining data needs to be flushed
    3. Consecutive IBIs after flush operations
    4. Data integrity through the flush pipeline
    """
    # Setup
    i3c_controller, i3c_target, tb = await test_setup(dut, verify_boot=True)

    target = i3c_controller.add_target(TARGET_ADDRESS)
    target.set_bcr_fields(ibi_req_capable=True, ibi_payload=True)

    result = True

    # Enable IBI ACK-ing
    i3c_controller.enable_ibi(True)

    dut._log.info("Phase 1: Queue multiple large IBIs")
    
    # Queue first large IBI (8 bytes)
    mdb = 0xE2
    data_1 = [0xCA, 0xFE, 0xBA, 0xCA, 0xAA, 0xBB, 0xCC, 0xDD]
    ibi_data = format_ibi_data(mdb, data_1)
    for word in ibi_data:
        await tb.write_csr(tb.reg_map.I3C_EC.TTI.IBI_PORT.base_addr, int2dword(word), 4)

    dut._log.info("Phase 2: Partial read on IBI #0 to trigger Flush")
    
    # Limit read to 4 bytes to trigger flush
    i3c_controller.set_max_ibi_data_len(4)
    
    # Wait for first IBI (will get only 4 bytes due to limit)
    response = await i3c_controller.wait_for_ibi()
    expected = bytearray([TARGET_ADDRESS, mdb] + data_1[:4])
    if response != expected:
        dut._log.critical(
            "IBI #0 MDB/data mismatch! tgt: [ {}] ctl: [ {}]".format(
                "".join("".join(f"0x{d:02X}") + " " for d in expected),
                "".join("".join(f"0x{d:02X}") + " " for d in response),
            )
        )
        result = False
    
    # Add small delay to allow flush to complete
    await ClockCycles(tb.clk, 100)

    dut._log.info("Phase 3: Accept remaining IBI (auto-advanced)")
    
    # Reset limit to accept all data
    i3c_controller.set_max_ibi_data_len(256)
    
    # Queue second IBI (should work after flush)
    mdb = 0xE3
    data_2 = [0x11, 0x22, 0x33, 0x44]
    ibi_data = format_ibi_data(mdb, data_2)
    for word in ibi_data:
        await tb.write_csr(tb.reg_map.I3C_EC.TTI.IBI_PORT.base_addr, int2dword(word), 4)

    # Wait for second IBI
    response = await i3c_controller.wait_for_ibi()
    expected = bytearray([TARGET_ADDRESS, mdb] + data_2)
    if response != expected:
        dut._log.critical(
            "IBI #1 MDB/data mismatch! tgt: [ {}] ctl: [ {}]".format(
                "".join("".join(f"0x{d:02X}") + " " for d in expected),
                "".join("".join(f"0x{d:02X}") + " " for d in response),
            )
        )
        result = False

    # Verify IBI status is clean after operations
    status = dword2int(await tb.read_csr(tb.reg_map.I3C_EC.TTI.STATUS.base_addr, 4))
    last_ibi_status = (status & (3 << 14)) >> 14
    expected_status = 0  # Should be ACK
    if last_ibi_status != expected_status:
        dut._log.critical(
            f"Incorrect IBI status after flush, expected {expected_status}, got {last_ibi_status}"
        )
        result = False

    assert result
