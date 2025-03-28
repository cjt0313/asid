import os
import sys
import warnings
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, TextIO, Tuple, Union

import numpy as np
import torch
from matplotlib import pyplot as plt

import wandb

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    SummaryWriter = None

DEBUG = 10
INFO = 20
WARN = 30
ERROR = 40
DISABLED = 50


class Video(object):
    """
    Video data class storing the video frames and the frame per seconds
    :param frames: frames to create the video from
    :param fps: frames per second
    """

    def __init__(self, frames: Union[torch.Tensor, np.ndarray], fps: Union[float, int]):
        self.frames = frames
        self.fps = fps


class Figure(object):
    """
    Figure data class storing a matplotlib figure and whether to close the figure after logging it
    :param figure: figure to log
    :param close: if true, close the figure after logging it
    """

    def __init__(self, figure: plt.figure, close: bool):
        self.figure = figure
        self.close = close


class Image(object):
    """
    Image data class storing an image and data format
    :param image: image to log
    :param dataformats: Image data format specification of the form NCHW, NHWC, CHW, HWC, HW, WH, etc.
        More info in add_image method doc at https://pytorch.org/docs/stable/tensorboard.html
        Gym envs normally use 'HWC' (channel last)
    """

    def __init__(self, image: Union[torch.Tensor, np.ndarray, str], dataformats: str):
        self.image = image
        self.dataformats = dataformats


class FormatUnsupportedError(NotImplementedError):
    def __init__(self, unsupported_formats: Sequence[str], value_description: str):
        if len(unsupported_formats) > 1:
            format_str = f"formats {', '.join(unsupported_formats)} are"
        else:
            format_str = f"format {unsupported_formats[0]} is"
        super(FormatUnsupportedError, self).__init__(
            f"The {format_str} not supported for the {value_description} value logged.\n"
            f"You can exclude formats via the `exclude` parameter of the logger's `record` function."
        )


class KVWriter(object):
    """
    Key Value writer
    """

    def write(
        self,
        key_values: Dict[str, Any],
        key_excluded: Dict[str, Union[str, Tuple[str, ...]]],
        step: int = 0,
    ) -> None:
        """
        Write a dictionary to file
        :param key_values:
        :param key_excluded:
        :param step:
        """
        raise NotImplementedError

    def close(self) -> None:
        """
        Close owned resources
        """
        raise NotImplementedError


class SeqWriter(object):
    """
    sequence writer
    """

    def write_sequence(self, sequence: List) -> None:
        """
        write_sequence an array to file
        :param sequence:
        """
        raise NotImplementedError


class HumanOutputFormat(KVWriter, SeqWriter):
    """A human-readable output format producing ASCII tables of key-value pairs.
    Set attribute `max_length` to change the maximum length of keys and values
    to write to output (or specify it when calling `__init__`).
    """

    def __init__(self, filename_or_file: Union[str, TextIO], max_length: int = 36):
        """
        log to a file, in a human readable format
        :param filename_or_file: the file to write the log to
        :param max_length: the maximum length of keys and values to write to output.
            Outputs longer than this will be truncated. An error will be raised
            if multiple keys are truncated to the same value. The maximum output
            width will be ``2*max_length + 7``. The default of 36 produces output
            no longer than 79 characters wide.
        """
        self.max_length = max_length
        if isinstance(filename_or_file, str):
            self.file = open(filename_or_file, "wt")
            self.own_file = True
        else:
            assert hasattr(
                filename_or_file, "write"
            ), f"Expected file or str, got {filename_or_file}"
            self.file = filename_or_file
            self.own_file = False

    def write(self, key_values: Dict, key_excluded: Dict, step: int = 0) -> None:
        # Create strings for printing
        key2str = []
        tag = None
        tags = set()
        for (key, value), (_, excluded) in zip(
            sorted(key_values.items()), sorted(key_excluded.items())
        ):

            if excluded is not None and ("stdout" in excluded or "log" in excluded):
                continue

            elif isinstance(value, Video):
                raise FormatUnsupportedError(["stdout", "log"], "video")

            elif isinstance(value, Figure):
                raise FormatUnsupportedError(["stdout", "log"], "figure")

            elif isinstance(value, Image):
                raise FormatUnsupportedError(["stdout", "log"], "image")

            elif isinstance(value, float):
                # Align left
                value_str = f"{value:<8.3g}"
            else:
                value_str = str(value)

            # Find tag and add it to the dict
            if key.find("/") > 0:
                tag = key[: key.find("/") + 1]
                if tag not in tags:
                    tags.add(tag)
                    key2str.append((self._truncate(tag), ""))
            # Remove tag from key
            if tag is not None and tag in key:
                key = str("   " + key[len(tag) :])
            # Add key to the dict
            key2str.append((self._truncate(key), self._truncate(value_str)))

        # Find max widths
        if len(key2str) == 0:
            warnings.warn("Tried to write empty key-value dict")
            return
        else:
            keys, vals = list(zip(*key2str))
            key_width = max(map(len, keys))
            val_width = max(map(len, vals))

        # Write out the data
        dashes = "-" * (key_width + val_width + 7)
        lines = [dashes]
        for key, value in key2str:
            key_space = " " * (key_width - len(key))
            val_space = " " * (val_width - len(value))
            lines.append(f"| {key}{key_space} | {value}{val_space} |")
        lines.append(dashes)
        self.file.write("\n".join(lines) + "\n")

        # Flush the output to the file
        self.file.flush()

    def _truncate(self, string: str) -> str:
        if len(string) > self.max_length:
            string = string[: self.max_length - 3] + "..."
        return string

    def write_sequence(self, sequence: List) -> None:
        sequence = list(sequence)
        for i, elem in enumerate(sequence):
            self.file.write(elem)
            if i < len(sequence) - 1:  # add space unless this is the last one
                self.file.write(" ")
        self.file.write("\n")
        self.file.flush()

    def close(self) -> None:
        """
        closes the file
        """
        if self.own_file:
            self.file.close()


class TensorBoardOutputFormat(KVWriter):
    def __init__(self, folder: str):
        """
        Dumps key/value pairs into TensorBoard's numeric format.
        :param folder: the folder to write the log to
        """
        assert SummaryWriter is not None, (
            "tensorboard is not installed, you can use "
            "pip install tensorboard to do so"
        )
        self.writer = SummaryWriter(log_dir=folder)

    def write(
        self,
        key_values: Dict[str, Any],
        key_excluded: Dict[str, Union[str, Tuple[str, ...]]],
        step: int = 0,
    ) -> None:

        for (key, value), (_, excluded) in zip(
            sorted(key_values.items()), sorted(key_excluded.items())
        ):

            if excluded is not None and "tensorboard" in excluded:
                continue

            if isinstance(value, np.ScalarType):
                if isinstance(value, str):
                    # str is considered a np.ScalarType
                    self.writer.add_text(key, value, step)
                else:
                    self.writer.add_scalar(key, value, step)

            if isinstance(value, torch.Tensor):
                self.writer.add_histogram(key, value, step)

            if isinstance(value, Video):
                self.writer.add_video(key, value.frames, step, value.fps)

            if isinstance(value, Figure):
                self.writer.add_figure(key, value.figure, step, close=value.close)

            if isinstance(value, Image):
                self.writer.add_image(
                    key, value.image, step, dataformats=value.dataformats
                )

        # Flush the output to the file
        self.writer.flush()

    def close(self) -> None:
        """
        closes the file
        """
        if self.writer:
            self.writer.close()
            self.writer = None


class WANDBOutputFormat(KVWriter):
    def __init__(self):
        if wandb.run is None:
            raise ValueError("WandB is not initialized")

    def write(
        self,
        key_values: Dict[str, Any],
        key_excluded: Dict[str, Union[str, Tuple[str, ...]]],
        step: int = 0,
    ) -> None:

        wandb_dict = {}
        for (key, value), (_, excluded) in zip(
            sorted(key_values.items()), sorted(key_excluded.items())
        ):

            if excluded is not None and "wandb" in excluded:
                continue

            if isinstance(value, np.ScalarType):
                wandb_dict[key] = value

            if isinstance(value, torch.Tensor):
                wandb_dict[key] = wandb.Histogram(value)

            if isinstance(value, Video):
                # T, C, H, W
                wandb_dict[key] = wandb.Video(value.frames, fps=value.fps, format="gif")
                pass
            if isinstance(value, Image):
                wandb_dict[key] = wandb.Image(value.image)

        # Log to wandb
        wandb.log(wandb_dict)

    def close(self) -> None:
        pass


def make_output_format(_format: str, log_dir: str, log_suffix: str = "") -> KVWriter:
    """
    return a logger for the requested format
    :param _format: the requested format to log to ('stdout', 'log', 'json' or 'csv' or 'tensorboard')
    :param log_dir: the logging directory
    :param log_suffix: the suffix for the log file
    :return: the logger
    """
    os.makedirs(log_dir, exist_ok=True)
    if _format == "stdout":
        return HumanOutputFormat(sys.stdout)
    elif _format == "log":
        return HumanOutputFormat(os.path.join(log_dir, f"log{log_suffix}.txt"))
    elif _format == "tensorboard":
        return TensorBoardOutputFormat(log_dir)
    elif _format == "wandb":
        return WANDBOutputFormat()
    else:
        raise ValueError(f"Unknown format specified: {_format}")


# ================================================================
# Backend
# ================================================================


class Logger(object):
    """
    The logger class.
    :param folder: the logging location
    :param output_formats: the list of output formats
    """

    def __init__(self, folder: Optional[str], output_formats: List[KVWriter]):
        self.name_to_value = defaultdict(float)  # values this iteration
        self.name_to_count = defaultdict(int)
        self.name_to_excluded = defaultdict(str)
        self.level = INFO
        self.dir = folder
        self.output_formats = output_formats

    def record(
        self,
        key: str,
        value: Any,
        exclude: Optional[Union[str, Tuple[str, ...]]] = None,
    ) -> None:
        """
        Log a value of some diagnostic
        Call this once for each diagnostic quantity, each iteration
        If called many times, last value will be used.
        :param key: save to log this key
        :param value: save to log this value
        :param exclude: outputs to be excluded
        """
        self.name_to_value[key] = value
        self.name_to_excluded[key] = exclude

    def record_mean(
        self,
        key: str,
        value: Any,
        exclude: Optional[Union[str, Tuple[str, ...]]] = None,
    ) -> None:
        """
        The same as record(), but if called many times, values averaged.
        :param key: save to log this key
        :param value: save to log this value
        :param exclude: outputs to be excluded
        """
        if value is None:
            self.name_to_value[key] = None
            return
        old_val, count = self.name_to_value[key], self.name_to_count[key]
        self.name_to_value[key] = old_val * count / (count + 1) + value / (count + 1)
        self.name_to_count[key] = count + 1
        self.name_to_excluded[key] = exclude

    def dump(self, step: int = 0) -> None:
        """
        Write all of the diagnostics from the current iteration
        """
        if self.level == DISABLED:
            return
        for _format in self.output_formats:
            if isinstance(_format, KVWriter):
                _format.write(self.name_to_value, self.name_to_excluded, step)

        self.name_to_value.clear()
        self.name_to_count.clear()
        self.name_to_excluded.clear()

    def log(self, *args, level: int = INFO) -> None:
        """
        Write the sequence of args, with no separators,
        to the console and output files (if you've configured an output file).
        level: int. (see logger.py docs) If the global logger level is higher than
                    the level argument here, don't print to stdout.
        :param args: log the arguments
        :param level: the logging level (can be DEBUG=10, INFO=20, WARN=30, ERROR=40, DISABLED=50)
        """
        if self.level <= level:
            self._do_log(args)

    def debug(self, *args) -> None:
        """
        Write the sequence of args, with no separators,
        to the console and output files (if you've configured an output file).
        Using the DEBUG level.
        :param args: log the arguments
        """
        self.log(*args, level=DEBUG)

    def info(self, *args) -> None:
        """
        Write the sequence of args, with no separators,
        to the console and output files (if you've configured an output file).
        Using the INFO level.
        :param args: log the arguments
        """
        self.log(*args, level=INFO)

    def warn(self, *args) -> None:
        """
        Write the sequence of args, with no separators,
        to the console and output files (if you've configured an output file).
        Using the WARN level.
        :param args: log the arguments
        """
        self.log(*args, level=WARN)

    def error(self, *args) -> None:
        """
        Write the sequence of args, with no separators,
        to the console and output files (if you've configured an output file).
        Using the ERROR level.
        :param args: log the arguments
        """
        self.log(*args, level=ERROR)

    # Configuration
    # ----------------------------------------
    def set_level(self, level: int) -> None:
        """
        Set logging threshold on current logger.
        :param level: the logging level (can be DEBUG=10, INFO=20, WARN=30, ERROR=40, DISABLED=50)
        """
        self.level = level

    def get_dir(self) -> str:
        """
        Get directory that log files are being written to.
        will be None if there is no output directory (i.e., if you didn't call start)
        :return: the logging directory
        """
        return self.dir

    def close(self) -> None:
        """
        closes the file
        """
        for _format in self.output_formats:
            _format.close()

    # Misc
    # ----------------------------------------
    def _do_log(self, args) -> None:
        """
        log to the requested format outputs
        :param args: the arguments to log
        """
        for _format in self.output_formats:
            if isinstance(_format, SeqWriter):
                _format.write_sequence(map(str, args))


def configure_logger(folder: str, format_strings: List[str] = None) -> Logger:
    """
    Configure the current logger.
    :param folder: the save location
    :param format_strings: the output logging format
    :return: The logger object.
    """
    assert isinstance(folder, str)
    os.makedirs(folder, exist_ok=True)

    log_suffix = ""
    format_strings = list(filter(None, format_strings))
    output_formats = [make_output_format(f, folder, log_suffix) for f in format_strings]

    logger = Logger(folder=folder, output_formats=output_formats)
    if len(format_strings) > 0 and format_strings != ["stdout"]:
        logger.log(f"Logging to {folder}")
    return logger
