import time
import pyvisa as pv
import numpy as np

class FPGAError(Exception):
    """Raised when FPGA Interface reports error."""


class FPGAInterface:
    """
    Serial (VISA/ASRL) interface to an Altera DE2-115 FPGA board.
    """

    # ---- Fixed serial link configuration ----
    BAUD_RATE = 19200
    DATA_BITS = 8
    STOP_BITS = pv.constants.StopBits.one
    PARITY = pv.constants.Parity.none
    TERMINATION_CHAR = 0xFF
    TIMEOUT_MS = 10000                # 10 s
    POST_OPEN_WAIT_S = 0.1            # 100 ms settle time after opening
    SAMPLE_PERIOD_S = 0.1             # base sampling period for acquire_counts

    def __init__(self):
        self._rm = pv.ResourceManager()
        self.connected_devices = list(self._rm.list_resources())
        self._fpga = None
        self.BYTES_TO_READ = 41

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def open(self, port: str) -> bool:
        """
        Open a serial connection to the FPGA board and configure it.
        Returns True on success.

        Raises
        ------
        FPGAError
            If connection failed.
        """
        try:
            self._fpga = self._rm.open_resource(port)
            self._fpga.clear()

            # --- serial line settings ---
            self._fpga.baud_rate = self.BAUD_RATE
            self._fpga.data_bits = self.DATA_BITS
            self._fpga.stop_bits = self.STOP_BITS
            self._fpga.parity = self.PARITY

            # --- termination / timeout ---
            self._fpga.read_termination = chr(self.TERMINATION_CHAR)
            self._fpga.timeout = self.TIMEOUT_MS  # ms

            # Let the board settle before the first read
            time.sleep(self.POST_OPEN_WAIT_S)

            return True

        except Exception as e:
            self._fpga = None
            raise FPGAError(
                f'Failed to connect to DE2-115. Check cable connection. Error: {e}'
            ) from e

    def close(self) -> None:
        """Close the serial connection if one is open."""
        if self._fpga is None:
            return
        try:
            self._fpga.close()
        except Exception as e:
            raise FPGAError(f'Error while closing connection to DE2-115. Error: {e}') from e
        finally:
            self._fpga = None
    
    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "FPGAInterface":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------
    # Low-level read
    # ------------------------------------------------------------------
    def read_data(self):
        """
        Read a single 0xFF-terminated raw byte string (41 bytes) from the FPGA.
        Returns the raw bytes.
         
        Raises
        ------
        FPGAError 
            If no active conection, VISA read error, or unexpected error.
        """
        if self._fpga is None:
            raise FPGAError('Cannot read: no active connection.')
        try:
            return self._fpga.read_bytes(self.BYTES_TO_READ)
        except pv.errors.VisaIOError as e:
            err = str(e)
            raise FPGAError(f'VISA read error while communicating with DE2-115. Error: {err}') from e
        except Exception as e:
            err = str(e)
            raise FPGAError(f'Unexpected error while reading from DE2-115. Error: {err}') from e

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

        Raises
        ------
        FPGAError
            If missing termination byte or wrong number of bytes received.
        """
        data = np.frombuffer(raw, dtype=np.uint8)

        if data.size == 0 or data[-1] != 0xFF:
            raise FPGAError("Missing 0xFF termination byte")
        data = data[:-1]  # drop terminator

        if data.size != 40:  # 8 counters * 5 bytes
            raise FPGAError(f"Expected 40 data bytes, got {data.size}")

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
        FPGAError
            If there is no open connection, a read/conversion fails, or
            update_period is not a positive multiple of 0.1 s.
        """
        if self._fpga is None:
            raise FPGAError('No active connection to DE2-115.')

        n_reads = round(update_period / self.SAMPLE_PERIOD_S)

        if n_reads <= 0 or not np.isclose(n_reads * self.SAMPLE_PERIOD_S, update_period):
            raise FPGAError(
                f'update_period must be a positive multiple of '
                f'{self.SAMPLE_PERIOD_S} s, got {update_period}'
            )

        total_counts = np.zeros(8, dtype=np.uint64)

        for i in range(n_reads):
            raw = self.read_data()
            if raw is None:
                raise FPGAError(
                    f'Failed to read data on sample {i + 1}/{n_reads}'
                )

            try:
                counts = self.altera_string_to_counts(raw)
            except FPGAError as e:
                raise FPGAError(
                    f'Malformed data on sample {i + 1}/{n_reads}: {e}'
                ) from e

            total_counts += counts
            time.sleep(self.SAMPLE_PERIOD_S)

        return total_counts.astype(np.uint32)

if __name__ == "__main__":
    with FPGAInterface() as dev:
        print('Available devices:', dev.connected_devices)
        dev.open(dev.connected_devices[0])
        print('Reading raw data:')
        print(dev.read_data())
        print('Acquiring counts (QIE specific):')
        print(dev.acquire_counts(1.0))  # 1 s acquisition window