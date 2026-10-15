"""Where the time of each reading of the quote goes: one CSV line per reading, and a summary.

The collector and the scan write, next to their own CSV, a file with the same name after
tempos-, such as tempos-scan-<data>-<hora>-<sufixo>.csv. It has one line for every reading
of the quote, also the readings that
were not kept, such as the scan's readings while the chart of a new asset was still
loading and its attempts to read the previous asset's quote before a click. Each line
tells the moment of the reading (observação, referência antes da troca, depois da troca,
sem troca), how long after the click it started, and the time of each step of the visual
reading: finding the chart element (count, visibility and position), the screenshot, the
decoding of the image, the detection of the marker and the OCR, with the size of the
image. result says whether a quote was read; verdict, in the scan, whether its checks
kept it, and why not. At the end of a run, the terminal shows the median and the largest
time of each step for each moment, so that a slow step, and when it is slow, can be
found without opening the file.

This module only writes what it is given: it never touches the browser.
"""

import csv
import statistics

TIMING_COLUMNS = (
    "captured_at_utc",  # this computer's clock, in UTC, right before the reading
    "moment",  # observação, prévia, referência antes da troca, depois da troca, sem troca
    "cycle",  # scan only
    "favorite_position",  # scan only
    "asset",  # the asset on the screen when the reading started, when known
    "since_click_ms",  # scan: from the click on the favorite to the start of the reading
    "attempt",  # the attempt or the reading of the data, from 1, within its visit
    "result",  # ok when a quote was read, or indisponível; it does not say that it was kept
    "quote",  # the quote read, when available
    "reason",  # why it is unavailable
    "verdict",  # scan only: aceita, or recusada: <why the scan's checks did not keep it>, or empty
    "source",  # texto or ocr
    "total_ms",
    "element_ms",  # count + visibility + position of the chart element
    "count_ms",
    "visible_ms",
    "box_ms",
    "screenshot_ms",
    "decode_ms",
    "detection_ms",
    "ocr_ms",
    "image_px",  # the captured image, in device pixels: width x height
    "png_bytes",
    "strip_css",  # width of the right strip captured, in CSS pixels; empty for the whole chart
)
# The steps shown in the summary, with their names in the terminal.
SUMMARY_STEPS = (
    ("total_ms", "total"),
    ("element_ms", "elemento"),
    ("screenshot_ms", "tela"),
    ("decode_ms", "decodificação"),
    ("detection_ms", "detecção"),
    ("ocr_ms", "OCR"),
)


PREFIX = "tempos-"
ACCEPTED, REFUSED = "aceita", "recusada"  # the verdict of the scan's checks on a quote it read


def timing_path(main_csv):
    """The timings file of a run: tempos- before the main CSV's name, in the same folder."""
    return main_csv.with_name(PREFIX + main_csv.name)


def cell(value):
    """A CSV cell: empty for None, one decimal for a float, the text otherwise."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


class TimingLog:
    """A new CSV file with TIMING_COLUMNS, flushed after every line."""

    def __init__(self, path):
        self.path = path
        self._file = open(path, "x", newline="", encoding="utf-8")
        try:
            self._writer = csv.writer(self._file)
            self._writer.writerow(TIMING_COLUMNS)
            self._file.flush()
        except BaseException:
            self._file.close()
            raise

    def write(self, row):
        """Write row, a dict with some of TIMING_COLUMNS; the others stay empty."""
        self._writer.writerow([cell(row.get(name)) for name in TIMING_COLUMNS])
        self._file.flush()

    def close(self):
        self._file.close()


class TimingRecorder:
    """The timings of one run: each reading goes to the summary and to a TimingLog.

    The file is created with the first reading. If it cannot be written, warn(message) is
    called once and the run goes on without it: the timings only explain the readings, and
    the summary stays in memory.
    """

    def __init__(self, path, warn):
        self.path = path
        self.summary = TimingSummary()
        self._warn = warn
        self._log = None
        self.failed = False

    @property
    def written(self):
        return self._log is not None

    def add(self, moment, row):
        row = dict(row, moment=moment)
        self.summary.add(moment, row)
        if self.failed:
            return
        try:
            if self._log is None:
                self._log = TimingLog(self.path)
            self._log.write(row)
        except OSError as error:
            self.failed = True
            self._warn(
                f"AVISO: não foi possível gravar os tempos das leituras em {self.path} ({error});"
                " a leitura continua sem esse arquivo, e o resumo dos tempos aparece no fim."
            )

    def close(self):
        if self._log is not None:
            self._log.close()


class TimingSummary:
    """The times of the readings of one run, grouped by moment, in the order they first came."""

    def __init__(self):
        self._samples = {}  # moment -> {step: [ms, ...]}
        self._counts = {}  # moment -> [readings, available, judged, accepted]

    def add(self, moment, row):
        steps = self._samples.setdefault(moment, {name: [] for name, _ in SUMMARY_STEPS})
        counts = self._counts.setdefault(moment, [0, 0, 0, 0])
        counts[0] += 1
        counts[1] += row.get("result") == "ok"
        verdict = row.get("verdict")
        counts[2] += bool(verdict)
        counts[3] += verdict == ACCEPTED
        for name, _ in SUMMARY_STEPS:
            value = row.get(name)
            if isinstance(value, (int, float)):
                steps[name].append(float(value))

    def lines(self):
        """The terminal lines: per moment, median and largest time of each step, in ms."""
        lines = []
        for moment, steps in self._samples.items():
            total, available, judged, accepted = self._counts[moment]
            parts = []
            for name, label in SUMMARY_STEPS:
                values = steps[name]
                if values:
                    parts.append(f"{label} {statistics.median(values):.0f}/{max(values):.0f}")
            counted = f"{total} leitura" + ("" if total == 1 else "s") + f", {available} com cotação"
            if judged:
                counted += f", {accepted} aceita" + ("" if accepted == 1 else "s") + " nas conferências"
            lines.append(f"  {moment} ({counted}): {', '.join(parts)}")
        return lines
