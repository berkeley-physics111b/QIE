import time
import pyvisa as pv
import numpy as np


class FPGAInterface:
    """
    Serial (VISA/ASRL) interface to an Altera DE2-115 FPGA board.
    """

    # ---- Fixed serial link configuration ----
    BAUD_RATE = 19200
    DATA_BITS = 8
    STOP_BITS = pv.constants.StopBits.one
    PARITY = pv.constants.Parity.none
    INPUT_BUFFER_SIZE = 128          # bytes
    RECV_BUFFER_MASK = 0xFFFF        # 16-bit receive buffer mask
    TERMINATION_CHAR = 0xFF
    TIMEOUT_MS = 10000                # 10 s
    POST_OPEN_WAIT_S = 0.1            # 100 ms settle time after opening
    SAMPLE_PERIOD_S = 0.1             # base sampling period for acquire_counts

    def __init__(self):
        # change to list to port options, gui select...?
        self.rm = pv.ResourceManager()
        self.connected_devices = self.rm.list_resources()
        self.fpga = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def open(self, port: str) -> bool:
        """
        Open a serial connection to the FPGA board and configure it.
        Returns True on success, False otherwise.
        """
        try:
            self.fpga = self.rm.open_resource(port)

            # --- serial line settings ---
            self.fpga.baud_rate = self.BAUD_RATE
            self.fpga.data_bits = self.DATA_BITS
            self.fpga.stop_bits = self.STOP_BITS
            self.fpga.parity = self.PARITY

            # --- termination / timeout ---
            self.fpga.read_termination = chr(self.TERMINATION_CHAR)
            self.fpga.timeout = self.TIMEOUT_MS  # ms

            # --- I/O buffer sizing (NI-VISA viSetBuffer) ---
            # Allocates read+write buffers of INPUT_BUFFER_SIZE bytes.
            self.fpga.set_buffer(
                pv.constants.VI_READ_BUF | pv.constants.VI_WRITE_BUF,
                self.INPUT_BUFFER_SIZE,
            )

            # --- 16-bit receive buffer mask ---
            # Governs how many pending-byte-count bits are tracked on the
            # ASRL receive queue; adjust if your VISA backend rejects this.
            self.fpga.set_visa_attribute(
                pv.constants.VI_ATTR_ASRL_AVAIL_NUM,
                self.RECV_BUFFER_MASK,
            )

            # Let the board settle before the first read
            time.sleep(self.POST_OPEN_WAIT_S)

            idn = self.fpga.query('*IDN?')
            print(f"Connected to: {idn.strip()}")
            return True

        except Exception as e:
            print('Failed to connect to DE2-115. Check cable connection.')
            print(e)
            self.fpga = None
            return False

    def close(self) -> None:
        """Close the serial connection if one is open."""
        if self.fpga is None:
            print('No active connection to close.')
            return
        try:
            self.fpga.close()
        except Exception as e:
            print('Error while closing connection to DE2-115.')
            print(e)
        finally:
            self.fpga = None

    # ------------------------------------------------------------------
    # Low-level read
    # ------------------------------------------------------------------
    def read_data(self):
        """
        Read a single 0xFF-terminated raw byte string from the FPGA.
        Returns the raw bytes, or None on failure.
        """
        if self.fpga is None:
            print('Cannot read: no active connection.')
            return None
        try:
            return self.fpga.read_raw()
        except pv.errors.VisaIOError as e:
            print('VISA read error while communicating with DE2-115.')
            print(e)
            return None
        except Exception as e:
            print('Unexpected error while reading from DE2-115.')
            print(e)
            return None

    # ------------------------------------------------------------------
    # Byte string -> counts conversion
    # ------------------------------------------------------------------
    @staticmethod
    def altera_string_to_counts(raw: bytes) -> np.ndarray:
        """
        Vectorized conversion of a raw byte string from the Altera board into
        8 counter values (uint32).

        Format: 8 counters x 5 bytes each (7 data bits/byte, MSB first),
        followed by a single 0xFF termination byte.
        """
        data = np.frombuffer(raw, dtype=np.uint8)

        if data.size == 0 or data[-1] != 0xFF:
            raise ValueError("Missing 0xFF termination byte")
        data = data[:-1]  # drop terminator

        if data.size != 40:  # 8 counters * 5 bytes
            raise ValueError(f"Expected 40 data bytes, got {data.size}")

        # reshape into 8 rows (counters) x 5 columns (bytes, MSB first)
        chunks = data.reshape(8, 5).astype(np.uint32)
        chunks &= 0x7F  # keep only the 7 data bits per byte

        # base-128 place values: [128^4, 128^3, 128^2, 128^1, 128^0]
        weights = (128 ** np.arange(4, -1, -1)).astype(np.uint32)

        # weighted sum per row -> one value per counter, wrapped to uint32
        counts = (chunks * weights).sum(axis=1, dtype=np.uint64) & 0xFFFFFFFF
        return counts.astype(np.uint32)

    # ------------------------------------------------------------------
    # High-level acquisition
    # ------------------------------------------------------------------
    def acquire_counts(self, update_period: float) -> np.ndarray:
        """
        Poll the FPGA every SAMPLE_PERIOD_S (0.1 s) for `update_period`
        seconds, convert each raw read into an 8-element counter array,
        and return the element-wise sum.

        Parameters
        ----------
        update_period : float
            Total acquisition window in seconds. Must be a positive
            multiple of SAMPLE_PERIOD_S (0.1 s).

        Raises
        ------
        RuntimeError
            If there is no open connection, or a read/conversion fails.
        ValueError
            If update_period is not a positive multiple of 0.1 s.
        """
        if self.fpga is None:
            raise RuntimeError('No active connection to DE2-115.')

        n_reads = round(update_period / self.SAMPLE_PERIOD_S)

        if n_reads <= 0 or not np.isclose(n_reads * self.SAMPLE_PERIOD_S, update_period):
            raise ValueError(
                f'update_period must be a positive multiple of '
                f'{self.SAMPLE_PERIOD_S} s, got {update_period}'
            )

        total_counts = np.zeros(8, dtype=np.uint64)

        for i in range(n_reads):
            raw = self.read_data()
            if raw is None:
                raise RuntimeError(
                    f'Failed to read data on sample {i + 1}/{n_reads}'
                )

            try:
                counts = self.altera_string_to_counts(raw)
            except ValueError as e:
                raise RuntimeError(
                    f'Malformed data on sample {i + 1}/{n_reads}: {e}'
                )

            total_counts += counts
            time.sleep(self.SAMPLE_PERIOD_S)

        return total_counts.astype(np.uint32)