# Copyright (c) 2010-2011, Emmanuel Blot <emmanuel.blot@free.fr>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the Neotion nor the names of its contributors may
#       be used to endorse or promote products derived from this software
#       without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL NEOTION BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import struct
import time
from pyftdi import Ftdi
from pyftdi.bits import BitSequence
from pyftdi.misc import hexline

__all__ = ['JtagEngine']


class JtagState(object):
    """Test Access Port controller state"""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name

    def setx(self, fstate, tstate):
        self.exits = [fstate, tstate]

    def getx(self, event):
        x = event and 1 or 0
        return self.exits[x]


class JtagStateMachine(object):
    """Test Access Port controller state machine"""

    def __init__(self):
        self.states = {}
        for s in ['test_logic_reset',
                  'run_test_idle',
                  'select_dr_scan',
                  'capture_dr',
                  'shift_dr',
                  'exit_1_dr',
                  'pause_dr',
                  'exit_2_dr',
                  'update_dr',
                  'select_ir_scan',
                  'capture_ir',
                  'shift_ir',
                  'exit_1_ir',
                  'pause_ir',
                  'exit_2_ir',
                  'update_ir']:
            self.states[s] = JtagState(s)
        self['test_logic_reset'].setx(self['run_test_idle'],
                                      self['test_logic_reset'])
        self['run_test_idle'].setx(self['run_test_idle'],
                                   self['select_dr_scan'])
        self['select_dr_scan'].setx(self['capture_dr'],
                                   self['select_ir_scan'])
        self['capture_dr'].setx(self['shift_dr'], self['exit_1_dr'])
        self['shift_dr'].setx(self['shift_dr'], self['exit_1_dr'])
        self['exit_1_dr'].setx(self['pause_dr'], self['update_dr'])
        self['pause_dr'].setx(self['pause_dr'], self['exit_2_dr'])
        self['exit_2_dr'].setx(self['shift_dr'], self['update_dr'])
        self['update_dr'].setx(self['run_test_idle'],
                               self['select_dr_scan'])
        self['select_ir_scan'].setx(self['capture_ir'],
                                    self['test_logic_reset'])
        self['capture_ir'].setx(self['shift_ir'], self['exit_1_ir'])
        self['shift_ir'].setx(self['shift_ir'], self['exit_1_ir'])
        self['exit_1_ir'].setx(self['pause_ir'], self['update_ir'])
        self['pause_ir'].setx(self['pause_ir'], self['exit_2_ir'])
        self['exit_2_ir'].setx(self['shift_ir'], self['update_ir'])
        self['update_ir'].setx(self['run_test_idle'], self['select_dr_scan'])

        self.reset()

    def __getitem__(self, name):
        return self.states[name]

    def state(self):
        return self._current

    def reset(self):
        self._current = self['test_logic_reset']

    def find_path(self, target, source=None):
        """Find the shortest event sequence to move from source state to
           target state. If source state is not specified, used the current
           state.
           Return the list of states, including source and target states"""
        if source is None:
            source = self.state()
        if isinstance(source, str):
            source = self[source]
        if isinstance(target, str):
            target = self[target]
        paths = []
        def next_path(state, target, path):
            # this test match the target, path is valid
            if state == target:
                return path+[state]
            # candidate paths
            paths = []
            for n,x in enumerate(state.exits):
                # next state is self (loop around), kill the path
                if x == state:
                    continue
                # next state already in upstream (loop back), kill the path
                if x in path:
                    continue
                # try the current path
                npath = next_path(x, target, path + [state])
                # downstream is a valid path, store it
                if npath:
                    paths.append(npath)
            # keep the shortest path
            return paths and min([(len(l), l) for l in paths],
                                  key=lambda x: x[0])[1] or []
        return next_path(source, target, [])

    def get_events(self, path):
        """Build up an event sequence from a state sequence, so that the
           resulting event sequence allows the JTAG state machine to advance
           from the first state to the last one of the input sequence"""
        events = []
        for s,d in zip(path[:-1], path[1:]):
            for e,x in enumerate(s.exits):
                if x == d:
                    events.append(e)
        if len(events) != len(path) - 1:
            raise AssertionError("Invalid path")
        return BitSequence(events)

    def handle_events(self, events):
        for event in events:
            self._current = self._current.getx(event)


class JtagController(object):
    """JTAG master of an FTDI device"""

    TCK_BIT = 0x01   # output
    TDI_BIT = 0x02   # output
    TDO_BIT = 0x04   # input
    TMS_BIT = 0x08   # output
    TRST_BIT = 0x10  # output, not available on 2232 JTAG debugger
    JTAG_MASK = 0x1f

    # Private API
    def __init__(self, logger):
        self.log = logger
        self.ftdi = Ftdi(self.log)
        self.direction = JtagController.TCK_BIT | \
                         JtagController.TDI_BIT | \
                         JtagController.TMS_BIT | \
                         JtagController.TRST_BIT
        self._last = 1
        self._write_buff = ''

    def _stack_cmd(self, cmd):
        if (len(self._write_buff) + len(cmd)) > 512:
            self.sync()
        self._write_buff += cmd

    def _write_bits(self, out):
        """Output bits on TDI"""
        length = len(out)
        byte = out.tobyte()
        cmd = struct.pack('<BBB', Ftdi.WRITE_BITS_NVE_LSB, length-1, byte)
        self.log.debug("Write TDI(%d): %s" % (len(cmd), hexline(cmd)))
        self._stack_cmd(cmd)

    def _write_bytes(self, out):
        """Output bytes on TDI"""
        bytes = out.tobytes(msby=True) # don't ask...
        cmd = struct.pack('<BH%dB' % len(bytes), Ftdi.WRITE_BYTES_NVE_LSB,
                          len(bytes)-1, *bytes)
        self.log.debug("Write TDI(%d): %s" % (len(out), hexline(cmd)))
        self._stack_cmd(cmd)

    # Public API
    def debug(self, level):
        self._debug = level

    def configure(self, vendor=0x0403, product=0x6011, interface=0,
                  highreset=False, frequency=3.0E6):
        """Configure the FTDI interface as a JTAG controller"""
        curfreq = self.ftdi.open_mpsse(vendor, product, interface,
                                       self.direction, frequency)
        self.log.debug( "JTAG freq req. %2.2e, real freq. %2.2e" % \
                       (frequency, curfreq))
        if highreset:
            value = 0x01
            direction = 0x01
            cmd = struct.pack('<BBB', Ftdi.SET_BITS_HIGH, value, direction)
            self.ftdi.write_data(cmd)
            self.ftdi.check_error()

    def terminate(self):
        if self.ftdi:
            self.ftdi.close()
            self.ftdi = None

    def reset(self):
        """Reset the attached TAP controller"""
        # we can either send a TRST HW signal or perform 5 cycles with TMS=1
        # to move the remote TAP controller back to 'test_logic_reset' state
        # do both for now
        #self.log.debug("HW (TRst) reset")
        if not self.ftdi:
            raise AssertionError("FTDI controller terminated")
        value = 0
        cmd = struct.pack('<BBB', Ftdi.SET_BITS_LOW, value, self.direction)
        self.ftdi.write_data(cmd)
        self.ftdi.check_error('HW reset')
        time.sleep(0.1)
        value = TRST_BIT
        cmd = struct.pack('<BBB', Ftdi.SET_BITS_LOW, value, self.direction)
        self.ftdi.write_data(cmd)
        self.ftdi.check_error('HW reset')
        time.sleep(0.1)
        self.log.debug("SW (TMS) reset")
        self.write_tms(BitSequence('11111'))
        self.ftdi.check_error('SW reset')

    def sync(self):
        if not self.ftdi:
            raise AssertionError("FTDI controller terminated")
        if self._write_buff:
            self.ftdi.write_data(self._write_buff)
            self._write_buff = ''

    def write_tms(self, out):
        """Change the TAP controller state"""
        length = len(out)
        self.log.debug("Last bit: %d" % self._last)
        if self._last:
            out = out[:]
            out[7] = 1
            self._last = 0
        cmd = struct.pack('<BBB', Ftdi.WRITE_BITS_TMS_NVE, length-1,
                          out.tobyte())
        self.log.debug("Write TMS: %s" % hexline(cmd))
        self._stack_cmd(cmd)

    def read_bits(self, length):
        """Read out bits from TDO"""
        if not self.ftdi:
            raise AssertionError("FTDI controller terminated")
        data = ''
        if length > 8:
            raise AssertionError, "Cannot fit into FTDI fifo"
        cmd = struct.pack('<BB', Ftdi.READ_BITS_NVE_LSB, length-1)
        self.log.debug("Write FTDI %s" % hexline(cmd))
        self._stack_cmd(cmd)
        self.sync()
        data = self.ftdi.read_data(1)
        self.log.debug("Read TDO: %s (%d)" % (hexline(data), len(data)))
        return BitSequence(ord(data), length=length)

    def read_bytes(self, length):
        """Read out bytes from TDO"""
        if not self.ftdi:
            raise AssertionError("FTDI controller terminated")
        data = ''
        if length > 512:
            raise AssertionError, "Cannot fit into FTDI fifo"
        cmd = struct.pack('<BH', Ftdi.READ_BYTES_NVE_LSB, length-1)
        self.log.debug("Write FTDI %s" % hexline(cmd))
        self._stack_cmd(cmd)
        self.sync()
        data = self.ftdi.read_data(length)
        self.log.debug("Read TDO: %s (%d)" % (hexline(data), len(data)))
        return BitSequence(bytes=data, length=8*length)

    def read_write_bytes(self, out):
        if not self.ftdi:
            raise AssertionError("FTDI controller terminated")
        length = len(out)
        cmd = struct.pack('<BH', Ftdi.RW_BYTES_NVE_LSB, length-1)
        cmd += out
        self.log.debug("Write TDI(%d): %s" % (length, hexline(cmd)))
        self._stack_cmd(cmd)
        self.sync()
        data = self.ftdi.read_data(length)
        self.log.debug("Read TDO(%d): %s" % (len(data), hexline(data)))

    def read(self, length):
        """Read out a sequence of bits from TDO"""
        byte_count = length//8
        bit_count = length-8*byte_count
        bs = BitSequence()
        if byte_count:
            bytes = self.read_bytes(byte_count)
            bs.append(bytes)
        if bit_count:
            bits = self.read_bits(bit_count)
            bs.append(bits)
        return bs

    def write(self, out):
        """Write a sequence of bits to TDI"""
        if isinstance(out, str):
            out = BitSequence(bytes=out)
        elif not isinstance(out, BitSequence):
            out = BitSequence(out)
        (out, self._last) = (out[:-1], int(out[-1]))
        byte_count = len(out)//8
        pos = 8*byte_count
        bit_count = len(out)-pos
        self.log.debug("POS: %d" % pos)
        if byte_count:
            self._write_bytes(out[:pos])
        if bit_count:
            self._write_bits(out[pos:])


class JtagEngine(object):
    """High-level JTAG engine controller"""

    def __init__(self, logger):
        self.log = logger
        self._ctrl = JtagController(logger)
        self._sm = JtagStateMachine()
        self._seq = ''

    def debug(self, level):
        """Change the current debug level"""
        self._ctrl.debug(level)

    def configure(self, vendor=0x0403, product=0x6011, interface=0,
                  frequency=3.0E6):
        """Configure the FTDI interface as a JTAG controller"""
        self._ctrl.configure(vendor=vendor, product=product,
                             interface=interface, frequency=frequency)

    def terminate(self):
        """Terminate a JTAG session/connection"""
        self._ctrl.terminate()

    def reset(self):
        """Reset the attached TAP controller"""
        self._ctrl.reset()
        self._sm.reset()

    def write_tms(self, out):
        """Change the TAP controller state"""

    def read(self, length):
        """Read out a sequence of bits from TDO"""
        return self._ctrl.read(length)

    def write(self, out):
        """Write a sequence of bits to TDI"""
        self._ctrl.write(out)

    def get_available_statenames(self):
        """Return a list of supported state name"""
        return [str(s) for s in self._sm.states]

    def change_state(self, statename):
        """Advance the TAP controller to the defined state"""
        # find the state machine path to move to the new instruction
        path = self._sm.find_path(statename)
        # convert the path into an event sequence
        events = self._sm.get_events(path)
        # update the remote device tap controller
        self._ctrl.write_tms(events)
        # update the current state machine's state
        self._sm.handle_events(events)

    def go_idle(self):
        """Change the current TAP controller to the IDLE state"""
        self.change_state('run_test_idle')

    def write_ir(self, instruction):
        """Change the current instruction of the TAP controller"""
        self.change_state('shift_ir')
        self._ctrl.write(instruction)
        self.change_state('update_ir')

    def write_dr(self, data):
        """Change the data register of the TAP controller"""
        self.change_state('shift_dr')
        self._ctrl.write(data)
        self.change_state('update_dr')

    def read_dr(self, length):
        """Read the data register from the TAP controller"""
        self.change_state('shift_dr')
        data = self._ctrl.read(length)
        self.change_state('update_dr')
        return data

    #Facility functions
    def idcode(self):
        idcode = self.read_dr(32)
        self.go_idle()
        return int(idcode)

    def preload(self, bsdl, data):
        instruction = bsdl.get_jtag_ir('preload')
        self.write_ir(instruction)
        self.write_dr(data)
        self.go_idle()

    def sample(self, bsdl):
        instruction = bsdl.get_jtag_ir('sample')
        self.write_ir(instruction)
        data = self.read_dr(bsdl.get_boundary_length())
        self.go_idle()
        return data

    def extest(self, bsdl):
        instruction = bsdl.get_jtag_ir('extest')
        self.write_ir(instruction)

    def readback(self, bsdl):
        data = self.read_dr(bsdl.get_boundary_length())
        self.go_idle()
        return data

    def sync(self):
        self._ctrl.sync()
