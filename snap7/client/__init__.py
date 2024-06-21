"""
Snap7 client used for connection to a siemens 7 server.
"""

import re
import logging
from ctypes import CFUNCTYPE, byref, create_string_buffer, sizeof
from ctypes import Array, _SimpleCData, c_byte, c_char_p, c_int, c_int32, c_uint16, c_ulong, c_void_p
from datetime import datetime
from typing import Any, Callable, Hashable, List, Optional, Tuple, Union, Type
from types import TracebackType

from ..common import check_error, ipv4, load_library
from ..protocol import Snap7CliProtocol
from ..types import S7SZL, Areas, BlocksList, S7CpInfo, S7CpuInfo, S7DataItem
from ..types import S7OrderCode, S7Protection, S7SZLList, TS7BlockInfo, WordLen
from ..types import S7Object, buffer_size, buffer_type, cpu_statuses, param_types
from ..types import RemotePort, wordlen_to_ctypes, block_types

logger = logging.getLogger(__name__)


def error_wrap(func: Callable[..., Any]) -> Callable[..., Any]:
    """Parses a s7 error code returned the decorated function."""

    def f(*args: tuple[Any, ...], **kwargs: dict[Hashable, Any]) -> None:
        code = func(*args, **kwargs)
        check_error(code, context="client")

    return f


class Client:
    """
    A snap7 client

    Examples:
        >>> import snap7
        >>> client = snap7.client.Client()
        >>> client.connect("127.0.0.1", 0, 0, 1102)
        >>> client.get_connected()
        True
        >>> data = client.db_read(1, 0, 4)
        >>> data
        bytearray(b"\\x00\\x00\\x00\\x00")
        >>> data[3] = 0b00000001
        >>> data
        bytearray(b'\\x00\\x00\\x00\\x01')
        >>> client.db_write(1, 0, data)
    """

    _lib: Snap7CliProtocol
    _read_callback = None
    _callback = None
    _s7_client: S7Object

    def __init__(self, lib_location: Optional[str] = None):
        """Creates a new `Client` instance.

        Args:
            lib_location: Full path to the snap7.dll file. Optional.

        Examples:
            >>> import snap7
            >>> client = snap7.client.Client()  # If the `snap7.dll` file is in the path location
            >>> client = snap7.client.Client(lib_location="/path/to/snap7.dll")  # If the `snap7.dll` file is in another location
            >>> client
            <snap7.client.Client object at 0x0000028B257128E0>
        """

        self._lib: Snap7CliProtocol = load_library(lib_location)
        self.create()

    def __enter__(self) -> "Client":
        return self

    def __exit__(
        self, exc_type: Optional[Type[BaseException]], exc_val: Optional[BaseException], exc_tb: Optional[TracebackType]
    ) -> None:
        self.destroy()

    def __del__(self) -> None:
        self.destroy()

    def create(self) -> None:
        """Creates a SNAP7 client."""
        logger.info("creating snap7 client")
        self._lib.Cli_Create.restype = S7Object  # type: ignore[attr-defined]
        self._s7_client = S7Object(self._lib.Cli_Create())

    def destroy(self) -> Optional[int]:
        """Destroys the Client object.

        Returns:
            Error code from snap7 library.

        Examples:
            >>> client.destroy()
            640719840
        """
        logger.info("destroying snap7 client")
        if self._lib and self._s7_client is not None:
            return self._lib.Cli_Destroy(byref(self._s7_client))
        self._s7_client = None  # type: ignore[assignment]
        return None

    def plc_stop(self) -> int:
        """Puts the CPU in STOP mode

        Returns:
            Error code from snap7 library.
        """
        logger.info("stopping plc")
        return self._lib.Cli_PlcStop(self._s7_client)

    def plc_cold_start(self) -> int:
        """Puts the CPU in RUN mode performing a COLD START.

        Returns:
            Error code from snap7 library.
        """
        logger.info("cold starting plc")
        return self._lib.Cli_PlcColdStart(self._s7_client)

    def plc_hot_start(self) -> int:
        """Puts the CPU in RUN mode performing an HOT START.

        Returns:
            Error code from snap7 library.
        """
        logger.info("hot starting plc")
        return self._lib.Cli_PlcHotStart(self._s7_client)

    def get_cpu_state(self) -> str:
        """Returns the CPU status (running/stopped)

        Returns:
            Description of the cpu state.

        Raises:
            :obj:`ValueError`: if the cpu state is invalid.

        Examples:
            >>> client.get_cpu_state()
            'S7CpuStatusRun'
        """
        state = c_int(0)
        self._lib.Cli_GetPlcStatus(self._s7_client, byref(state))
        try:
            status_string = cpu_statuses[state.value]
        except KeyError:
            raise ValueError(f"The cpu state ({state.value}) is invalid")

        logger.debug(f"CPU state is {status_string}")
        return status_string

    def get_cpu_info(self) -> S7CpuInfo:
        """Returns some information about the AG.

        Returns:
            :obj:`S7CpuInfo`: data structure with the information.

        Examples:
            >>> cpu_info = client.get_cpu_info()
            >>> print(cpu_info)
            "<S7CpuInfo ModuleTypeName: b'CPU 315-2 PN/DP'
                SerialNumber: b'S C-C2UR28922012'
                ASName: b'SNAP7-SERVER' Copyright: b'Original Siemens Equipment'
                ModuleName: b'CPU 315-2 PN/DP'>
        """
        info = S7CpuInfo()
        result = self._lib.Cli_GetCpuInfo(self._s7_client, byref(info))
        check_error(result, context="client")
        return info

    @error_wrap
    def disconnect(self) -> int:
        """Disconnect a client.

        Returns:
            Error code from snap7 library.
        """
        logger.info("disconnecting snap7 client")
        return self._lib.Cli_Disconnect(self._s7_client)

    @error_wrap
    def connect(self, address: str, rack: int, slot: int, tcpport: int = 102) -> int:
        """Connects a Client Object to a PLC.

        Args:
            address: IP address of the PLC.
            rack: rack number where the PLC is located.
            slot: slot number where the CPU is located.
            tcpport: port of the PLC.

        Returns:
            Error code from snap7 library.

        Example:
            >>> import snap7
            >>> client = snap7.client.Client()
            >>> client.connect("192.168.0.1", 0, 0)  # port is implicit = 102.
        """
        logger.info(f"connecting to {address}:{tcpport} rack {rack} slot {slot}")

        self.set_param(number=RemotePort, value=tcpport)
        return self._lib.Cli_ConnectTo(self._s7_client, c_char_p(address.encode()), c_int(rack), c_int(slot))

    def db_read(self, db_number: int, start: int, size: int) -> bytearray:
        """Reads a part of a DB from a PLC

        Note:
            Use it only for reading DBs, not Marks, Inputs, Outputs.

        Args:
            db_number: number of the DB to be read.
            start: byte index from where is start to read from.
            size: amount of bytes to be read.

        Returns:
            Buffer read.

        Example:
            >>> import snap7
            >>> client = snap7.client.Client()
            >>> client.connect("192.168.0.1", 0, 0)
            >>> buffer = client.db_read(1, 10, 4)  # reads the db number 1 starting from the byte 10 until byte 14.
            >>> buffer
            bytearray(b'\\x00\\x00')
        """
        logger.debug(f"db_read, db_number:{db_number}, start:{start}, size:{size}")

        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        data = (type_ * size)()
        result = self._lib.Cli_DBRead(self._s7_client, db_number, start, size, byref(data))
        check_error(result, context="client")
        return bytearray(data)

    @error_wrap
    def db_write(self, db_number: int, start: int, data: bytearray) -> int:
        """Writes a part of a DB into a PLC.

        Args:
            db_number: number of the DB to be read.
            start: byte index to start writing to.
            data: buffer to be write.

        Returns:
            Buffer written.

        Example:
            >>> import snap7
            >>> client = snap7.client.Client()
            >>> client.connect("192.168.0.1", 0, 0)
            >>> buffer = bytearray([0b00000001])
            >>> client.db_write(1, 10, buffer)  # writes the bit number 0 from the byte 10 to TRUE.
        """
        wordlen = WordLen.Byte
        type_ = wordlen_to_ctypes[wordlen.value]
        size = len(data)
        cdata = (type_ * size).from_buffer_copy(data)
        logger.debug(f"db_write db_number:{db_number} start:{start} size:{size} data:{data}")
        return self._lib.Cli_DBWrite(self._s7_client, db_number, start, size, byref(cdata))

    def delete(self, block_type: str, block_num: int) -> int:
        """Delete a block into AG.

        Args:
            block_type: type of block.
            block_num: block number.

        Returns:
            Error code from snap7 library.
        """
        logger.info("deleting block")
        blocktype = block_types[block_type]
        result = self._lib.Cli_Delete(self._s7_client, blocktype, block_num)
        return result

    def full_upload(self, _type: str, block_num: int) -> Tuple[bytearray, int]:
        """Uploads a block from AG with Header and Footer infos.
        The whole block (including header and footer) is copied into the user
        buffer.

        Args:
            _type: type of block.
            block_num: number of block.

        Returns:
            Tuple of the buffer and size.
        """
        _buffer = buffer_type()
        size = c_int(sizeof(_buffer))
        block_type = block_types[_type]
        result = self._lib.Cli_FullUpload(self._s7_client, block_type, block_num, byref(_buffer), byref(size))
        check_error(result, context="client")
        return bytearray(_buffer)[: size.value], size.value

    def upload(self, block_num: int) -> bytearray:
        """Uploads a block from AG.

        Note:
            Upload means from the PLC to the PC.

        Args:
            block_num: block to be upload.

        Returns:
            Buffer with the uploaded block.
        """
        logger.debug(f"db_upload block_num: {block_num}")
        block_type = block_types["DB"]
        _buffer = buffer_type()
        size = c_int(sizeof(_buffer))

        result = self._lib.Cli_Upload(self._s7_client, block_type, block_num, byref(_buffer), byref(size))

        check_error(result, context="client")
        logger.info(f"received {size} bytes")
        return bytearray(_buffer)

    @error_wrap
    def download(self, data: bytearray, block_num: int = -1) -> int:
        """Download a block into AG.
        A whole block (including header and footer) must be available into the
        user buffer.

        Note:
            Download means from the PC to the PLC.

        Args:
            data: buffer data.
            block_num: new block number.

        Returns:
            Error code from snap7 library.
        """
        type_ = c_byte
        size = len(data)
        cdata = (type_ * len(data)).from_buffer_copy(data)
        return self._lib.Cli_Download(self._s7_client, block_num, byref(cdata), size)

    def db_get(self, db_number: int) -> bytearray:
        """Uploads a DB from AG using DBRead.

        Note:
            This method can't be use for 1200/1500 PLCs.

        Args:
            db_number: db number to be read from.

        Returns:
            Buffer with the data read.

        Example:
            >>> import snap7
            >>> client = snap7.client.Client()
            >>> client.connect("192.168.0.1", 0, 0)
            >>> buffer = client.db_get(1)  # reads the db number 1.
            >>> buffer
            bytearray(b"\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00...<truncated>\\x00\\x00")
        """
        logger.debug(f"db_get db_number: {db_number}")
        _buffer = buffer_type()
        result = self._lib.Cli_DBGet(self._s7_client, db_number, byref(_buffer), byref(c_int(buffer_size)))
        check_error(result, context="client")
        return bytearray(_buffer)

    def read_area(self, area: Areas, dbnumber: int, start: int, size: int) -> bytearray:
        """Reads a data area from a PLC
                With it you can read DB, Inputs, Outputs, Merkers, Timers and Counters.

                Args:
                    area: area to be read from.
                    dbnumber: number of the db to be read from. In case of Inputs, Marks or Outputs, this should be equal to 0.
                    start: byte index to start reading.
                    size: number of bytes to read.

                Returns:
                    Buffer with the data read.

                Raises:
                    :obj:`ValueError`: if the area is not defined in the `Areas`

                Example:
        import snap7.util.db            >>> import snap7
                    >>> client = snap7.client.Client()
                    >>> client.connect("192.168.0.1", 0, 0)
                    >>> buffer = client.read_area(snap7.util.db.DB, 1, 10, 4)  # Reads the DB number 1 from the byte 10 to the byte 14.
                    >>> buffer
                    bytearray(b'\\x00\\x00')
        """
        if area not in Areas:
            raise ValueError(f"{area} is not implemented in types")
        elif area == Areas.TM:
            wordlen = WordLen.Timer
        elif area == Areas.CT:
            wordlen = WordLen.Counter
        else:
            wordlen = WordLen.Byte
        type_ = wordlen_to_ctypes[wordlen.value]
        logger.debug(
            f"reading area: {area.name} dbnumber: {dbnumber} start: {start} amount: {size} "
            f"wordlen: {wordlen.name}={wordlen.value}"
        )
        data = (type_ * size)()
        result = self._lib.Cli_ReadArea(self._s7_client, area.value, dbnumber, start, size, wordlen.value, byref(data))
        check_error(result, context="client")
        return bytearray(data)

    @error_wrap
    def write_area(self, area: Areas, dbnumber: int, start: int, data: bytearray) -> int:
        """Writes a data area into a PLC.

        Args:
            area: area to be write.
            dbnumber: number of the db to be write to. In case of Inputs, Marks or Outputs, this should be equal to 0.
            start: byte index to start writting.
            data: buffer to be write.

        Returns:
            Snap7 error code.

        Exmaple:
            >>> import snap7.util.db
            >>> import snap7
            >>> client = snap7.client.Client()
            >>> client.connect("192.168.0.1", 0, 0)
            >>> buffer = bytearray([0b00000001])
            # Writes the bit 0 of the byte 10 from the DB number 1 to TRUE.
            >>> client.write_area(snap7.util.DB, 1, 10, buffer)
        """
        if area == Areas.TM:
            wordlen = WordLen.Timer
        elif area == Areas.CT:
            wordlen = WordLen.Counter
        else:
            wordlen = WordLen.Byte
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        size = len(data)
        logger.debug(
            f"writing area: {area.name} dbnumber: {dbnumber} start: {start}: size {size}: "
            f"wordlen {wordlen.name}={wordlen.value} type: {type_}"
        )
        cdata = (type_ * len(data)).from_buffer_copy(data)
        return self._lib.Cli_WriteArea(self._s7_client, area.value, dbnumber, start, size, wordlen.value, byref(cdata))

    def read_multi_vars(self, items: Array[S7DataItem]) -> Tuple[int, Array[S7DataItem]]:
        """Reads different kind of variables from a PLC simultaneously.

        Args:
            items: list of items to be read.

        Returns:
            Tuple with the return code from the snap7 library and the list of items.
        """
        result = self._lib.Cli_ReadMultiVars(self._s7_client, byref(items), c_int32(len(items)))
        check_error(result, context="client")
        return result, items

    def list_blocks(self) -> BlocksList:
        """Returns the AG blocks amount divided by type.

        Returns:
            Block list structure object.

        Examples:
            >>> block_list = client.list_blocks()
            >>> print(block_list)
            <block list count OB: 0 FB: 0 FC: 0 SFB: 0 SFC: 0x0 DB: 1 SDB: 0>
        """
        logger.debug("listing blocks")
        blocksList = BlocksList()
        result = self._lib.Cli_ListBlocks(self._s7_client, byref(blocksList))
        check_error(result, context="client")
        logger.debug(f"blocks: {blocksList}")
        return blocksList

    def list_blocks_of_type(self, blocktype: str, size: int) -> Union[int, Array[c_uint16]]:
        """This function returns the AG list of a specified block type.

        Args:
            blocktype: specified block type.
            size: size of the block type.

        Returns:
            If size is 0, it returns a 0, otherwise an `Array` of specified block type.

        Raises:
            :obj:`ValueError`: if the `blocktype` is not valid.
        """

        _blocktype = block_types.get(blocktype)
        if not _blocktype:
            raise ValueError("The blocktype parameter was invalid")

        logger.debug(f"listing blocks of type: {_blocktype} size: {size}")

        if size == 0:
            return 0

        data = (c_uint16 * size)()
        count = c_int(size)
        result = self._lib.Cli_ListBlocksOfType(self._s7_client, _blocktype, byref(data), byref(count))

        logger.debug(f"number of items found: {count}")

        check_error(result, context="client")
        return data

    def get_block_info(self, blocktype: str, db_number: int) -> TS7BlockInfo:
        """Returns detailed information about a block present in AG.

        Args:
            blocktype: specified block type.
            db_number: number of db to get information from.

        Returns:
            Structure of information from block.

        Raises:
            :obj:`ValueError`: if the `blocktype` is not valid.

        Examples:
            >>> block_info = client.get_block_info("DB", 1)
            >>> print(block_info)
            Block type: 10
            Block number: 1
            Block language: 5
            Block flags: 1
            MC7Size: 100
            Load memory size: 192
            Local data: 0
            SBB Length: 20
            Checksum: 0
            Version: 1
            Code date: b'1999/11/17'
            Interface date: b'1999/11/17'
            Author: b''
            Family: b''
            Header: b''
        """
        blocktype_ = block_types.get(blocktype)

        if not blocktype_:
            raise ValueError("The blocktype parameter was invalid")
        logger.debug(f"retrieving block info for block {db_number} of type {blocktype_}")

        data = TS7BlockInfo()

        result = self._lib.Cli_GetAgBlockInfo(self._s7_client, blocktype_, db_number, byref(data))
        check_error(result, context="client")
        return data

    @error_wrap
    def set_session_password(self, password: str) -> int:
        """Send the password to the PLC to meet its security level.

        Args:
            password: password to set.

        Returns:
            Snap7 code.

        Raises:
            :obj:`ValueError`: if the length of the `password` is more than 8 characters.
        """
        if len(password) > 8:
            raise ValueError("Maximum password length is 8")
        return self._lib.Cli_SetSessionPassword(self._s7_client, c_char_p(password.encode()))

    @error_wrap
    def clear_session_password(self) -> int:
        """Clears the password set for the current session (logout).

        Returns:
            Snap7 code.
        """
        return self._lib.Cli_ClearSessionPassword(self._s7_client)

    def set_connection_params(self, address: str, local_tsap: int, remote_tsap: int) -> None:
        """Sets internally (IP, LocalTSAP, RemoteTSAP) Coordinates.

        Note:
            This function must be called just before `Cli_Connect()`.

        Args:
            address: PLC/Equipment IPV4 Address, for example "192.168.1.12"
            local_tsap: Local TSAP (PC TSAP)
            remote_tsap: Remote TSAP (PLC TSAP)

        Raises:
            :obj:`ValueError`: if the `address` is not a valid IPV4.
            :obj:`ValueError`: if the result of setting the connection params is
                different than 0.
        """
        if not re.match(ipv4, address):
            raise ValueError(f"{address} is invalid ipv4")
        result = self._lib.Cli_SetConnectionParams(self._s7_client, address.encode(), c_uint16(local_tsap), c_uint16(remote_tsap))
        if result != 0:
            raise ValueError("The parameter was invalid")

    def set_connection_type(self, connection_type: int) -> None:
        """Sets the connection resource type, i.e the way in which the Clients connects to a PLC.

        Args:
            connection_type: 1 for PG, 2 for OP, 3 to 10 for S7 Basic

        Raises:
            :obj:`ValueError`: if the result of setting the connection type is
                different than 0.
        """
        result = self._lib.Cli_SetConnectionType(self._s7_client, c_uint16(connection_type))
        if result != 0:
            raise ValueError("The parameter was invalid")

    def get_connected(self) -> bool:
        """Returns the connection status

        Note:
            Sometimes returns True, while connection is lost.

        Returns:
            True if is connected, otherwise false.
        """
        connected = c_int32()
        result = self._lib.Cli_GetConnected(self._s7_client, byref(connected))
        check_error(result, context="client")
        return bool(connected)

    def ab_read(self, start: int, size: int) -> bytearray:
        """Reads a part of IPU area from a PLC.

        Args:
            start: byte index from where start to read.
            size: amount of bytes to read.

        Returns:
            Buffer with the data read.
        """
        wordlen = WordLen.Byte
        type_ = wordlen_to_ctypes[wordlen.value]
        data = (type_ * size)()
        logger.debug(f"ab_read: start: {start}: size {size}: ")
        result = self._lib.Cli_ABRead(self._s7_client, start, size, byref(data))
        check_error(result, context="client")
        return bytearray(data)

    def ab_write(self, start: int, data: bytearray) -> int:
        """Writes a part of IPU area into a PLC.

        Args:
            start: byte index from where start to write.
            data: buffer with the data to be written.

        Returns:
            Snap7 code.
        """
        wordlen = WordLen.Byte
        type_ = wordlen_to_ctypes[wordlen.value]
        size = len(data)
        cdata = (type_ * size).from_buffer_copy(data)
        logger.debug(f"ab write: start: {start}: size: {size}: ")
        return self._lib.Cli_ABWrite(self._s7_client, start, size, byref(cdata))

    def as_ab_read(self, start: int, size: int, data: "Array[_SimpleCData[Any]]") -> int:
        """Reads a part of IPU area from a PLC asynchronously.

        Args:
            start: byte index from where start to read.
            size: amount of bytes to read.
            data: buffer where the data will be place.

        Returns:
            Snap7 code.
        """
        logger.debug(f"ab_read: start: {start}: size {size}: ")
        result = self._lib.Cli_AsABRead(self._s7_client, start, size, byref(data))
        check_error(result, context="client")
        return result

    def as_ab_write(self, start: int, data: bytearray) -> int:
        """Writes a part of IPU area into a PLC asynchronously.

        Args:
            start: byte index from where start to write.
            data: buffer with the data to be written.

        Returns:
            Snap7 code.
        """
        wordlen = WordLen.Byte
        type_ = wordlen_to_ctypes[wordlen.value]
        size = len(data)
        cdata = (type_ * size).from_buffer_copy(data)
        logger.debug(f"ab write: start: {start}: size: {size}: ")
        result = self._lib.Cli_AsABWrite(self._s7_client, start, size, byref(cdata))
        check_error(result, context="client")
        return result

    def as_compress(self, time: int) -> int:
        """Performs the Compress action asynchronously.

        Args:
            time: timeout.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsCompress(self._s7_client, time)
        check_error(result, context="client")
        return result

    def as_copy_ram_to_rom(self, timeout: int = 1) -> int:
        """Performs the Copy Ram to Rom action asynchronously.

        Args:
            timeout: time to wait unly fail.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsCopyRamToRom(self._s7_client, timeout)
        check_error(result, context="client")
        return result

    def as_ct_read(self, start: int, amount: int, data: "Array[_SimpleCData[Any]]") -> int:
        """Reads counters from a PLC asynchronously.

        Args:
            start: byte index to start to read from.
            amount: amount of bytes to read.
            data: buffer where the value read will be place.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsCTRead(self._s7_client, start, amount, byref(data))
        check_error(result, context="client")
        return result

    def as_ct_write(self, start: int, amount: int, data: bytearray) -> int:
        """Write counters into a PLC.

        Args:
            start: byte index to start to write from.
            amount: amount of bytes to write.
            data: buffer to be write.

        Returns:
            Snap7 code.
        """
        type_ = wordlen_to_ctypes[WordLen.Counter.value]
        cdata = (type_ * amount).from_buffer_copy(data)
        result = self._lib.Cli_AsCTWrite(self._s7_client, start, amount, byref(cdata))
        check_error(result, context="client")
        return result

    def as_db_fill(self, db_number: int, filler: int) -> int:
        """Fills a DB in AG with a given byte.

        Args:
            db_number: number of DB to fill.
            filler: buffer to fill with.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsDBFill(self._s7_client, db_number, filler)
        check_error(result, context="client")
        return result

    def as_db_get(self, db_number: int, _buffer: "Array[_SimpleCData[Any]]", size: "_SimpleCData[Any]") -> int:
        """Uploads a DB from AG using DBRead.

        Note:
            This method will not work in 1200/1500.

        Args:
            db_number: number of DB to get.
            _buffer: buffer where the data read will be place.
            size: amount of bytes to be read.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsDBGet(self._s7_client, db_number, byref(_buffer), byref(size))
        check_error(result, context="client")
        return result

    def as_db_read(self, db_number: int, start: int, size: int, data: "Array[_SimpleCData[Any]]") -> int:
        """Reads a part of a DB from a PLC.

        Args:
            db_number: number of DB to be read.
            start: byte index from where start to read from.
            size: amount of bytes to read.
            data: buffer where the data read will be place.

        Returns:
            Snap7 code.

        Examples:
            >>> import ctypes
            >>> data = (ctypes.c_uint8 * size_to_read)()  # In this ctypes array data will be stored.
            >>> result = client.as_db_read(1, 0, size_to_read, data)
            >>> result  # 0 = success
            0
        """
        result = self._lib.Cli_AsDBRead(self._s7_client, db_number, start, size, byref(data))
        check_error(result, context="client")
        return result

    def as_db_write(self, db_number: int, start: int, size: int, data: "Array[_SimpleCData[Any]]") -> int:
        """Writes a part of a DB into a PLC.

        Args:
            db_number: number of DB to be write.
            start: byte index from where start to write to.
            size: amount of bytes to write.
            data: buffer to be write.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsDBWrite(self._s7_client, db_number, start, size, byref(data))
        check_error(result, context="client")
        return result

    def as_download(self, data: bytearray, block_num: int) -> int:
        """Download a block into AG asynchronously.

        Note:
            A whole block (including header and footer) must be available into the user buffer.

        Args:
            block_num: new block number.
            data: buffer where the data will be place.

        Returns:
            Snap7 code.
        """
        size = len(data)
        type_ = c_byte * len(data)
        cdata = type_.from_buffer_copy(data)
        result = self._lib.Cli_AsDownload(self._s7_client, block_num, byref(cdata), size)
        check_error(result)
        return result

    @error_wrap
    def compress(self, time: int) -> int:
        """Performs the Compress action.

        Args:
            time: timeout.

        Returns:
            Snap7 code.
        """
        return self._lib.Cli_Compress(self._s7_client, time)

    @error_wrap
    def set_param(self, number: int, value: int) -> int:
        """Writes an internal Server Parameter.

        Args:
            number: number of argument to be written.
            value: value to be written.

        Returns:
            Snap7 code.
        """
        logger.debug(f"setting param number {number} to {value}")
        type_ = param_types[number]
        return self._lib.Cli_SetParam(self._s7_client, number, byref(type_(value)))

    def get_param(self, number: int) -> int:
        """Reads an internal Server parameter.

        Args:
            number: number of argument to be read.

        Return:
            Value of the param read.
        """
        logger.debug(f"retreiving param number {number}")
        type_ = param_types[number]
        value = type_()
        code = self._lib.Cli_GetParam(self._s7_client, c_int(number), byref(value))
        check_error(code)
        return value.value

    def get_pdu_length(self) -> int:
        """Returns info about the PDU length (requested and negotiated).

        Returns:
            PDU length.

        Examples:
            >>> client.get_pdu_length()
            480
        """
        logger.info("getting PDU length")
        requested_ = c_uint16()
        negotiated_ = c_uint16()
        code = self._lib.Cli_GetPduLength(self._s7_client, byref(requested_), byref(negotiated_))
        check_error(code)
        return negotiated_.value

    def get_plc_datetime(self) -> datetime:
        """Returns the PLC date/time.

        Returns:
            Date and time as datetime

        Examples:
            >>> client.get_plc_datetime()
            datetime.datetime(2021, 4, 6, 12, 12, 36)
        """
        type_ = c_int32
        buffer = (type_ * 9)()
        result = self._lib.Cli_GetPlcDateTime(self._s7_client, byref(buffer))
        check_error(result, context="client")

        return datetime(
            year=buffer[5] + 1900, month=buffer[4] + 1, day=buffer[3], hour=buffer[2], minute=buffer[1], second=buffer[0]
        )

    @error_wrap
    def set_plc_datetime(self, dt: datetime) -> int:
        """Sets the PLC date/time with a given value.

        Args:
            dt: datetime to be set.

        Returns:
            Snap7 code.
        """
        type_ = c_int32
        buffer = (type_ * 9)()
        buffer[0] = dt.second
        buffer[1] = dt.minute
        buffer[2] = dt.hour
        buffer[3] = dt.day
        buffer[4] = dt.month - 1
        buffer[5] = dt.year - 1900

        return self._lib.Cli_SetPlcDateTime(self._s7_client, byref(buffer))

    def check_as_completion(self, p_value: c_int) -> int:
        """Method to check Status of an async request. Result contains if the check was successful, not the data value itself

        Args:
            p_value: Pointer where result of this check shall be written.

        Returns:
            Snap7 code. If 0 - Job is done successfully. If 1 - Job is either pending or contains s7errors
        """
        result = self._lib.Cli_CheckAsCompletion(self._s7_client, byref(p_value))
        check_error(result, context="client")
        return result

    def set_as_callback(self, call_back: Callable[..., Any]) -> int:
        """
        Sets the user callback that is called when a asynchronous data sent is complete.

        """
        logger.info("setting event callback")
        callback_wrap: Callable[..., Any] = CFUNCTYPE(None, c_void_p, c_int, c_int)

        def wrapper(usrptr: Optional[c_void_p], op_code: int, op_result: int) -> int:
            """Wraps python function into a ctypes function

            Args:
                usrptr: not used
                op_code:
                op_result:

            Returns:
                Should return an int
            """
            logger.info(f"callback event: op_code: {op_code} op_result: {op_result}")
            call_back(op_code, op_result)
            return 0

        self._callback = callback_wrap(wrapper)
        usrPtr = c_void_p()

        result = self._lib.Cli_SetAsCallback(self._s7_client, self._callback, usrPtr)
        check_error(result, context="client")
        return result

    def wait_as_completion(self, timeout: int) -> int:
        """Snap7 Cli_WaitAsCompletion representative.

        Args:
            timeout: ms to wait for async job

        Returns:
            Snap7 code.
        """
        # Cli_WaitAsCompletion
        result = self._lib.Cli_WaitAsCompletion(self._s7_client, c_ulong(timeout))
        check_error(result, context="client")
        return result

    def _prepare_as_read_area(self, area: Areas, size: int) -> Tuple[WordLen, "Array[_SimpleCData[int]]"]:
        if area not in Areas:
            raise ValueError(f"{area} is not implemented in types")
        elif area == Areas.TM:
            wordlen = WordLen.Timer
        elif area == Areas.CT:
            wordlen = WordLen.Counter
        else:
            wordlen = WordLen.Byte
        type_ = wordlen_to_ctypes[wordlen.value]
        usrdata = (type_ * size)()
        return wordlen, usrdata

    def as_read_area(
        self, area: Areas, dbnumber: int, start: int, size: int, wordlen: WordLen, pusrdata: "Array[_SimpleCData[Any]]"
    ) -> int:
        """Reads a data area from a PLC asynchronously.
        With it you can read DB, Inputs, Outputs, Merkers, Timers and Counters.

        Args:
            area: memory area to be read from.
            dbnumber: The DB number, only used when area=Areas.DB
            start: offset to start writing
            size: number of units to read
            pusrdata: buffer where the data will be place.
            wordlen: length of the word to be read.

        Returns:
            Snap7 code.
        """
        logger.debug(
            f"reading area: {area.name} dbnumber: {dbnumber} start: {start} amount: {size} "
            f"wordlen: {wordlen.name}={wordlen.value}"
        )
        result = self._lib.Cli_AsReadArea(self._s7_client, area.value, dbnumber, start, size, wordlen.value, byref(pusrdata))
        check_error(result, context="client")
        return result

    def _prepare_as_write_area(self, area: Areas, data: bytearray) -> Tuple[WordLen, "Array[_SimpleCData[Any]]"]:
        if area not in Areas:
            raise ValueError(f"{area} is not implemented in types")
        elif area == Areas.TM:
            wordlen = WordLen.Timer
        elif area == Areas.CT:
            wordlen = WordLen.Counter
        else:
            wordlen = WordLen.Byte
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        cdata = (type_ * len(data)).from_buffer_copy(data)
        return wordlen, cdata

    def as_write_area(
        self, area: Areas, dbnumber: int, start: int, size: int, wordlen: WordLen, pusrdata: "Array[_SimpleCData[Any]]"
    ) -> int:
        """Writes a data area into a PLC asynchronously.

        Args:
            area: memory area to be written.
            dbnumber: The DB number, only used when area=Areas.DB
            start: offset to start writing.
            size: amount of bytes to be written.
            wordlen: length of the word to be written.
            pusrdata: buffer to be written.

        Returns:
            Snap7 code.
        """
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        logger.debug(
            f"writing area: {area.name} dbnumber: {dbnumber} start: {start}: size {size}: " f"wordlen {wordlen} type: {type_}"
        )
        cdata = (type_ * len(pusrdata)).from_buffer_copy(pusrdata)
        res = self._lib.Cli_AsWriteArea(self._s7_client, area.value, dbnumber, start, size, wordlen.value, byref(cdata))
        check_error(res, context="client")
        return res

    def as_eb_read(self, start: int, size: int, data: "Array[_SimpleCData[Any]]") -> int:
        """Reads a part of IPI area from a PLC asynchronously.

        Args:
            start: byte index from where to start reading from.
            size: amount of bytes to read.
            data: buffer where the data read will be place.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsEBRead(self._s7_client, start, size, byref(data))
        check_error(result, context="client")
        return result

    def as_eb_write(self, start: int, size: int, data: bytearray) -> int:
        """Writes a part of IPI area into a PLC.

        Args:
            start: byte index from where to start writing from.
            size: amount of bytes to write.
            data: buffer to write.

        Returns:
            Snap7 code.
        """
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        cdata = (type_ * size).from_buffer_copy(data)
        result = self._lib.Cli_AsEBWrite(self._s7_client, start, size, byref(cdata))
        check_error(result, context="client")
        return result

    def as_full_upload(self, _type: str, block_num: int) -> int:
        """Uploads a block from AG with Header and Footer infos.

        Note:
            Upload means from PLC to PC.

        Args:
            _type: type of block.
            block_num: number of block to upload.

        Returns:
            Snap7 code.
        """
        _buffer = buffer_type()
        size = c_int(sizeof(_buffer))
        block_type = block_types[_type]
        result = self._lib.Cli_AsFullUpload(self._s7_client, block_type, block_num, byref(_buffer), byref(size))
        check_error(result, context="client")
        return result

    def as_list_blocks_of_type(self, blocktype: str, data: "Array[_SimpleCData[Any]]", count: "_SimpleCData[Any]") -> int:
        """Returns the AG blocks list of a given type.

        Args:
            blocktype: block type.
            data: buffer where the data will be place.
            count: pass.

        Returns:
            Snap7 code.

        Raises:
            :obj:`ValueError`: if the `blocktype` is invalid
        """
        _blocktype = block_types.get(blocktype)
        if not _blocktype:
            raise ValueError("The blocktype parameter was invalid")
        result = self._lib.Cli_AsListBlocksOfType(self._s7_client, _blocktype, byref(data), byref(count))
        check_error(result, context="client")
        return result

    def as_mb_read(self, start: int, size: int, data: "Array[_SimpleCData[Any]]") -> int:
        """Reads a part of Merkers area from a PLC.

        Args:
            start: byte index from where to start to read from.
            size: amount of byte to read.
            data: buffer where the data read will be place.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsMBRead(self._s7_client, start, size, byref(data))
        check_error(result, context="client")
        return result

    def as_mb_write(self, start: int, size: int, data: bytearray) -> int:
        """Writes a part of Merkers area into a PLC.

        Args:
            start: byte index from where to start to write to.
            size: amount of byte to write.
            data: buffer to write.

        Returns:
            Snap7 code.
        """
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        cdata = (type_ * size).from_buffer_copy(data)
        result = self._lib.Cli_AsMBWrite(self._s7_client, start, size, byref(cdata))
        check_error(result, context="client")
        return result

    def as_read_szl(self, ssl_id: int, index: int, s7_szl: S7SZL, size: "_SimpleCData[Any]") -> int:
        """Reads a partial list of given ID and Index.

        Args:
            ssl_id: TODO
            index: TODO
            s7_szl: TODO
            size: TODO

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsReadSZL(self._s7_client, ssl_id, index, byref(s7_szl), byref(size))
        check_error(result, context="client")
        return result

    def as_read_szl_list(self, szl_list: S7SZLList, items_count: "_SimpleCData[Any]") -> int:
        """Reads the list of partial lists available in the CPU.

        Args:
            szl_list: TODO
            items_count: TODO

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsReadSZLList(self._s7_client, byref(szl_list), byref(items_count))
        check_error(result, context="client")
        return result

    def as_tm_read(self, start: int, amount: int, data: "Array[_SimpleCData[Any]]") -> int:
        """Reads timers from a PLC.

        Args:
            start: byte index to start read from.
            amount: amount of bytes to read.
            data: buffer where the data will be placed.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_AsTMRead(self._s7_client, start, amount, byref(data))
        check_error(result, context="client")
        return result

    def as_tm_write(self, start: int, amount: int, data: bytearray) -> int:
        """Write timers into a PLC.

        Args:
            start: byte index to start writing to.
            amount: amount of bytes to write.
            data: buffer to write.

        Returns:
            Snap7 code.
        """
        type_ = wordlen_to_ctypes[WordLen.Timer.value]
        cdata = (type_ * amount).from_buffer_copy(data)
        result = self._lib.Cli_AsTMWrite(self._s7_client, start, amount, byref(cdata))
        check_error(result)
        return result

    def as_upload(self, block_num: int, _buffer: "Array[_SimpleCData[Any]]", size: "_SimpleCData[Any]") -> int:
        """Uploads a block from AG.

        Note:
            Uploads means from PLC to PC.

        Args:
            block_num: block number to upload.
            _buffer: buffer where the data will be place.
            size: amount of bytes to uplaod.

        Returns:
            Snap7 code.
        """
        block_type = block_types["DB"]
        result = self._lib.Cli_AsUpload(self._s7_client, block_type, block_num, byref(_buffer), byref(size))
        check_error(result, context="client")
        return result

    def copy_ram_to_rom(self, timeout: int = 1) -> int:
        """Performs the Copy Ram to Rom action.

        Args:
            timeout: timeout time.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_CopyRamToRom(self._s7_client, timeout)
        check_error(result, context="client")
        return result

    def ct_read(self, start: int, amount: int) -> bytearray:
        """Reads counters from a PLC.

        Args:
            start: byte index to start read from.
            amount: amount of bytes to read.

        Returns:
            Buffer read.
        """
        type_ = wordlen_to_ctypes[WordLen.Counter.value]
        data = (type_ * amount)()
        result = self._lib.Cli_CTRead(self._s7_client, start, amount, byref(data))
        check_error(result, context="client")
        return bytearray(data)

    def ct_write(self, start: int, amount: int, data: bytearray) -> int:
        """Write counters into a PLC.

        Args:
            start: byte index to start write to.
            amount: amount of bytes to write.
            data: buffer data to write.

        Returns:
            Snap7 code.
        """
        type_ = wordlen_to_ctypes[WordLen.Counter.value]
        cdata = (type_ * amount).from_buffer_copy(data)
        result = self._lib.Cli_CTWrite(self._s7_client, start, amount, byref(cdata))
        check_error(result)
        return result

    def db_fill(self, db_number: int, filler: int) -> int:
        """Fills a DB in AG with a given byte.

        Args:
            db_number: db number to fill.
            filler: value filler.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_DBFill(self._s7_client, db_number, filler)
        check_error(result)
        return result

    def eb_read(self, start: int, size: int) -> bytearray:
        """Reads a part of IPI area from a PLC.

        Args:
            start: byte index to start read from.
            size: amount of bytes to read.

        Returns:
            Data read.
        """
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        data = (type_ * size)()
        result = self._lib.Cli_EBRead(self._s7_client, start, size, byref(data))
        check_error(result, context="client")
        return bytearray(data)

    def eb_write(self, start: int, size: int, data: bytearray) -> int:
        """Writes a part of IPI area into a PLC.

        Args:
            start: byte index to be written.
            size: amount of bytes to write.
            data: data to write.

        Returns:
            Snap7 code.
        """
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        cdata = (type_ * size).from_buffer_copy(data)
        result = self._lib.Cli_EBWrite(self._s7_client, start, size, byref(cdata))
        check_error(result)
        return result

    def error_text(self, error: int) -> str:
        """Returns a textual explanation of a given error number.

        Args:
            error: error number.

        Returns:
            Text error.
        """
        text_length = c_int(256)
        error_code = c_int32(error)
        text = create_string_buffer(buffer_size)
        response = self._lib.Cli_ErrorText(error_code, text, text_length)
        check_error(response)
        result = bytearray(text)[: text_length.value].decode().strip("\x00")
        return result

    def get_cp_info(self) -> S7CpInfo:
        """Returns some information about the CP (communication processor).

        Returns:
            Structure object containing the CP information.
        """
        cp_info = S7CpInfo()
        result = self._lib.Cli_GetCpInfo(self._s7_client, byref(cp_info))
        check_error(result)
        return cp_info

    def get_exec_time(self) -> int:
        """Returns the last job execution time in milliseconds.

        Returns:
            Execution time value.
        """
        time = c_int32()
        result = self._lib.Cli_GetExecTime(self._s7_client, byref(time))
        check_error(result)
        return time.value

    def get_last_error(self) -> int:
        """Returns the last job result.

        Returns:
            Returns the last error value.
        """
        last_error = c_int32()
        result = self._lib.Cli_GetLastError(self._s7_client, byref(last_error))
        check_error(result)
        return last_error.value

    def get_order_code(self) -> S7OrderCode:
        """Returns the CPU order code.

        Returns:
            Order of the code in a structure object.
        """
        order_code = S7OrderCode()
        result = self._lib.Cli_GetOrderCode(self._s7_client, byref(order_code))
        check_error(result)
        return order_code

    def get_pg_block_info(self, block: bytearray) -> TS7BlockInfo:
        """Returns detailed information about a block loaded in memory.

        Args:
            block: buffer where the data will be place.

        Returns:
            Structure object that contains the block information.
        """
        block_info = TS7BlockInfo()
        size = c_int(len(block))
        buffer = (c_byte * len(block)).from_buffer_copy(block)
        result = self._lib.Cli_GetPgBlockInfo(self._s7_client, byref(buffer), byref(block_info), size)
        check_error(result)
        return block_info

    def get_protection(self) -> S7Protection:
        """Gets the CPU protection level info.

        Returns:
            Structure object with protection attributes.
        """
        s7_protection = S7Protection()
        result = self._lib.Cli_GetProtection(self._s7_client, byref(s7_protection))
        check_error(result)
        return s7_protection

    def iso_exchange_buffer(self, data: bytearray) -> bytearray:
        """Exchanges a given S7 PDU (protocol data unit) with the CPU.

        Args:
            data: buffer to exchange.

        Returns:
            Snap7 code.
        """
        size = c_int(len(data))
        cdata = (c_byte * len(data)).from_buffer_copy(data)
        response = self._lib.Cli_IsoExchangeBuffer(self._s7_client, byref(cdata), byref(size))
        check_error(response)
        result = bytearray(cdata)[: size.value]
        return result

    def mb_read(self, start: int, size: int) -> bytearray:
        """Reads a part of Merkers area from a PLC.

        Args:
            start: byte index to be read from.
            size: amount of bytes to read.

        Returns:
            Buffer with the data read.
        """
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        data = (type_ * size)()
        result = self._lib.Cli_MBRead(self._s7_client, start, size, byref(data))
        check_error(result, context="client")
        return bytearray(data)

    def mb_write(self, start: int, size: int, data: bytearray) -> int:
        """Writes a part of Merkers area into a PLC.

        Args:
            start: byte index to be written.
            size: amount of bytes to write.
            data: buffer to write.

        Returns:
            Snap7 code.
        """
        type_ = wordlen_to_ctypes[WordLen.Byte.value]
        cdata = (type_ * size).from_buffer_copy(data)
        result = self._lib.Cli_MBWrite(self._s7_client, start, size, byref(cdata))
        check_error(result)
        return result

    def read_szl(self, ssl_id: int, index: int = 0x0000) -> S7SZL:
        """Reads a partial list of given ID and Index.

        Args:
            ssl_id: ssl id to be read.
            index: index to be read.

        Returns:
            SZL structure object.
        """
        s7_szl = S7SZL()
        size = c_int(sizeof(s7_szl))
        result = self._lib.Cli_ReadSZL(self._s7_client, ssl_id, index, byref(s7_szl), byref(size))
        check_error(result, context="client")
        return s7_szl

    def read_szl_list(self) -> bytearray:
        """Reads the list of partial lists available in the CPU.

        Returns:
            Buffer read.
        """
        szl_list = S7SZLList()
        items_count = c_int(sizeof(szl_list))
        response = self._lib.Cli_ReadSZLList(self._s7_client, byref(szl_list), byref(items_count))
        check_error(response, context="client")
        result = bytearray(szl_list.List)[: items_count.value]
        return result

    def set_plc_system_datetime(self) -> int:
        """Sets the PLC date/time with the host (PC) date/time.

        Returns:
            Snap7 code.
        """
        result = self._lib.Cli_SetPlcSystemDateTime(self._s7_client)
        check_error(result)
        return result

    def tm_read(self, start: int, amount: int) -> bytearray:
        """Reads timers from a PLC.

        Args:
            start: byte index from where is start to read from.
            amount: amount of byte to be read.

        Returns:
            Buffer read.
        """
        wordlen = WordLen.Timer
        type_ = wordlen_to_ctypes[wordlen.value]
        data = (type_ * amount)()
        result = self._lib.Cli_TMRead(self._s7_client, start, amount, byref(data))
        check_error(result, context="client")
        return bytearray(data)

    def tm_write(self, start: int, amount: int, data: bytearray) -> int:
        """Write timers into a PLC.

        Args:
            start: byte index from where is start to write to.
            amount: amount of byte to be written.
            data: data to be write.

        Returns:
            Snap7 code.
        """
        wordlen = WordLen.Timer
        type_ = wordlen_to_ctypes[wordlen.value]
        cdata = (type_ * amount).from_buffer_copy(data)
        result = self._lib.Cli_TMWrite(self._s7_client, start, amount, byref(cdata))
        check_error(result)
        return result

    def write_multi_vars(self, items: List[S7DataItem]) -> int:
        """Writes different kind of variables into a PLC simultaneously.

        Args:
            items: list of items to be written.

        Returns:
            Snap7 code.
        """
        items_count = c_int32(len(items))
        data = bytearray()
        for item in items:
            data += bytearray(item)
        cdata = (S7DataItem * len(items)).from_buffer_copy(data)
        result = self._lib.Cli_WriteMultiVars(self._s7_client, byref(cdata), items_count)
        check_error(result, context="client")
        return result
