import io
import json
import logging
import math
import os
import pprint
import re
import sys
import time

from collections import OrderedDict
from html import unescape
from html.parser import HTMLParser
from itertools import chain
from typing import Any, Callable, Optional, Dict, List, Pattern, Iterable, Tuple, Union
from urllib.request import Request
from urllib.request import urlopen
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode

import xml.etree.ElementTree as ElementTree

import requests

import pyperclip

logger = logging.getLogger(__name__)


class Caption:
    """Container for caption tracks."""

    def __init__(self, caption_track: Dict):
        """Construct a :class:`Caption <Caption>`.

        :param dict caption_track:
            Caption track data extracted from ``watch_html``.
        """
        self.url = caption_track.get("baseUrl")
        self.name = caption_track["name"]["simpleText"]
        self.code = caption_track["languageCode"]

    @property
    def xml_captions(self) -> str:
        """Download the xml caption tracks."""
        return get(self.url)

    def generate_srt_captions(self) -> str:
        """Generate "SubRip Subtitle" captions.

        Takes the xml captions from :meth:`~pytube.Caption.xml_captions` and
        recompiles them into the "SubRip Subtitle" format.
        """
        return self.xml_caption_to_srt(self.xml_captions)

    @staticmethod
    def float_to_srt_time_format(d: float) -> str:
        """Convert decimal durations into proper srt format.

        :rtype: str
        :returns:
            SubRip Subtitle (str) formatted time duration.

        float_to_srt_time_format(3.89) -> '00:00:03,890'
        """
        fraction, whole = math.modf(d)
        time_fmt = time.strftime("%H:%M:%S,", time.gmtime(whole))
        ms = f"{fraction:.3f}".replace("0.", "")
        return time_fmt + ms

    def xml_caption_to_srt(self, xml_captions: str) -> str:
        """Convert xml caption tracks to "SubRip Subtitle (srt)".

        :param str xml_captions:
            XML formatted caption tracks.
        """
        segments = []
        root = ElementTree.fromstring(xml_captions)
        for i, child in enumerate(list(root)):
            text = child.text or ""
            caption = unescape(text.replace("\n", " ").replace("  ", " "),)
            duration = float(child.attrib["dur"])
            start = float(child.attrib["start"])
            end = start + duration
            sequence_number = i + 1  # convert from 0-indexed to 1.
            line = "{seq}\n{start} --> {end}\n{text}\n".format(
                seq=sequence_number,
                start=self.float_to_srt_time_format(start),
                end=self.float_to_srt_time_format(end),
                text=caption,
            )
            segments.append(line)
        return "\n".join(segments).strip()

    def download(
        self,
        title: str,
        srt: bool = True,
        output_path: Optional[str] = None,
        filename_prefix: Optional[str] = None,
    ) -> str:
        """Write the media stream to disk.

        :param title:
            Output filename (stem only) for writing media file.
            If one is not specified, the default filename is used.
        :type title: str
        :param srt:
            Set to True to download srt, false to download xml. Defaults to True.
        :type srt bool
        :param output_path:
            (optional) Output path for writing media file. If one is not
            specified, defaults to the current working directory.
        :type output_path: str or None
        :param filename_prefix:
            (optional) A string that will be prepended to the filename.
            For example a number in a playlist or the name of a series.
            If one is not specified, nothing will be prepended
            This is separate from filename so you can use the default
            filename but still add a prefix.
        :type filename_prefix: str or None

        :rtype: str

        """
        if title.endswith(".srt") or title.endswith(".xml"):
            filename = ".".join(title.split(".")[:-1])
        else:
            filename = title

        if filename_prefix:
            filename = f"{safe_filename(filename_prefix)}{filename}"

        filename = safe_filename(filename)

        filename += f" ({self.code})"

        if srt:
            filename += ".srt"
        else:
            filename += ".xml"

        file_path = os.path.join(target_directory(output_path), filename)

        with open(file_path, "w", encoding="utf-8") as file_handle:
            if srt:
                file_handle.write(self.generate_srt_captions())
            else:
                file_handle.write(self.xml_captions)

        return file_path

    def __repr__(self):
        """Printable object representation."""
        return '<Caption lang="{s.name}" code="{s.code}">'.format(s=self)


class Monostate:
    def __init__(self):
        self.on_progress = None
        self.on_complete = None


def headers(url: str) -> Dict:
    """Fetch headers returned http GET request.

    :param str url:
        The URL to perform the GET request for.
    :rtype: dict
    :returns:
        dictionary of lowercase headers
    """
    return {k.lower(): v for k, v in _execute_request(url).info().items()}

class Stream:
    """Container for stream manifest data."""

    def __init__(self, stream: Dict, player_config_args: Dict, monostate: Monostate):
        """Construct a :class:`Stream <Stream>`.

        :param dict stream:
            The unscrambled data extracted from YouTube.
        :param dict player_config_args:
            The data object containing video media data like title and
            keywords.
        :param dict monostate:
            Dictionary of data shared across all instances of
            :class:`Stream <Stream>`.
        """
        # A dictionary shared between all instances of :class:`Stream <Stream>`
        # (Borg pattern).
        self._monostate = monostate

        self.url = stream["url"]  # signed download url
        self.itag = int(stream["itag"])  # stream format id (youtube nomenclature)

        # set type and codec info

        # 'video/webm; codecs="vp8, vorbis"' -> 'video/webm', ['vp8', 'vorbis']
        self.mime_type, self.codecs = mime_type_codec(stream["type"])

        # 'video/webm' -> 'video', 'webm'
        self.type, self.subtype = self.mime_type.split("/")

        # ['vp8', 'vorbis'] -> video_codec: vp8, audio_codec: vorbis. DASH
        # streams return NoneType for audio/video depending.
        self.video_codec, self.audio_codec = self.parse_codecs()

        self._filesize: Optional[int] = None  # filesize in bytes

        # Additional information about the stream format, such as resolution,
        # frame rate, and whether the stream is live (HLS) or 3D.
        itag_profile = get_format_profile(self.itag)
        self.is_dash = itag_profile["is_dash"]
        self.abr = itag_profile["abr"]  # average bitrate (audio streams only)
        self.fps = itag_profile["fps"]  # frames per second (video streams only)
        self.resolution = itag_profile["resolution"]  # resolution (e.g.: "480p")
        self.is_3d = itag_profile["is_3d"]
        self.is_hdr = itag_profile["is_hdr"]
        self.is_live = itag_profile["is_live"]

        # The player configuration, contains info like the video title.
        self.player_config_args = player_config_args

    @property
    def is_adaptive(self) -> bool:
        """Whether the stream is DASH.

        :rtype: bool
        """
        # if codecs has two elements (e.g.: ['vp8', 'vorbis']): 2 % 2 = 0
        # if codecs has one element (e.g.: ['vp8']) 1 % 2 = 1
        return bool(len(self.codecs) % 2)

    @property
    def is_progressive(self) -> bool:
        """Whether the stream is progressive.

        :rtype: bool
        """
        return not self.is_adaptive

    @property
    def includes_audio_track(self) -> bool:
        """Whether the stream only contains audio.

        :rtype: bool
        """
        return self.is_progressive or self.type == "audio"

    @property
    def includes_video_track(self) -> bool:
        """Whether the stream only contains video.

        :rtype: bool
        """
        return self.is_progressive or self.type == "video"

    def parse_codecs(self) -> Tuple[Optional[str], Optional[str]]:
        """Get the video/audio codecs from list of codecs.

        Parse a variable length sized list of codecs and returns a
        constant two element tuple, with the video codec as the first element
        and audio as the second. Returns None if one is not available
        (adaptive only).

        :rtype: tuple
        :returns:
            A two element tuple with audio and video codecs.

        """
        video = None
        audio = None
        if not self.is_adaptive:
            video, audio = self.codecs
        elif self.includes_video_track:
            video = self.codecs[0]
        elif self.includes_audio_track:
            audio = self.codecs[0]
        return video, audio

    @property
    def filesize(self) -> int:
        """File size of the media stream in bytes.

        :rtype: int
        :returns:
            Filesize (in bytes) of the stream.
        """
        if self._filesize is None:
            rheaders = headers(self.url)
            self._filesize = int(rheaders["content-length"])
        return self._filesize

    @property
    def title(self) -> str:
        """Get title of video

        :rtype: str
        :returns:
            Youtube video title
        """
        return (
            self.player_config_args.get("title")
            or (
                self.player_config_args.get("player_response", {})
                .get("videoDetails", {})
                .get("title")
            )
            or "Unknown YouTube Video Title"
        )

    @property
    def default_filename(self) -> str:
        """Generate filename based on the video title.

        :rtype: str
        :returns:
            An os file system compatible filename.
        """

        filename = safe_filename(self.title)
        return f"{filename}.{self.subtype}"

    def download(
        self,
        output_path: Optional[str] = None,
        filename: Optional[str] = None,
        filename_prefix: Optional[str] = None,
        skip_existing: bool = True,
    ) -> str:
        """Write the media stream to disk.

        :param output_path:
            (optional) Output path for writing media file. If one is not
            specified, defaults to the current working directory.
        :type output_path: str or None
        :param filename:
            (optional) Output filename (stem only) for writing media file.
            If one is not specified, the default filename is used.
        :type filename: str or None
        :param filename_prefix:
            (optional) A string that will be prepended to the filename.
            For example a number in a playlist or the name of a series.
            If one is not specified, nothing will be prepended
            This is separate from filename so you can use the default
            filename but still add a prefix.
        :type filename_prefix: str or None
        :param skip_existing:
            (optional) skip existing files, defaults to True
        :type skip_existing: bool
        :returns:
            Path to the saved video
        :rtype: str

        """
        if filename:
            filename = f"{safe_filename(filename)}.{self.subtype}"
        else:
            filename = self.default_filename

        if filename_prefix:
            filename = f"{safe_filename(filename_prefix)}{filename}"

        file_path = os.path.join(target_directory(output_path), filename)

        if (
            skip_existing
            and os.path.isfile(file_path)
            and os.path.getsize(file_path) == self.filesize
        ):
            # likely the same file, so skip it
            logger.debug("file %s already exists, skipping", file_path)
            return file_path

        bytes_remaining = self.filesize
        logger.debug(
            "downloading (%s total bytes) file to %s", self.filesize, file_path,
        )

        with open(file_path, "wb") as fh:
            for chunk in stream(self.url):
                # reduce the (bytes) remainder by the length of the chunk.
                bytes_remaining -= len(chunk)
                # send to the on_progress callback.
                self.on_progress(chunk, fh, bytes_remaining)
        self.on_complete(fh)
        return file_path

    def stream_to_buffer(self) -> io.BytesIO:
        """Write the media stream to buffer

        :rtype: io.BytesIO buffer
        """
        buffer = io.BytesIO()
        bytes_remaining = self.filesize
        logger.debug(
            "downloading (%s total bytes) file to BytesIO buffer", self.filesize,
        )

        for chunk in stream(self.url):
            # reduce the (bytes) remainder by the length of the chunk.
            bytes_remaining -= len(chunk)
            # send to the on_progress callback.
            self.on_progress(chunk, buffer, bytes_remaining)
        self.on_complete(buffer)
        return buffer

    def on_progress(self, chunk, file_handler, bytes_remaining):
        """On progress callback function.

        This function writes the binary data to the file, then checks if an
        additional callback is defined in the monostate. This is exposed to
        allow things like displaying a progress bar.

        :param bytes chunk:
            Segment of media file binary data, not yet written to disk.
        :param file_handler:
            The file handle where the media is being written to.
        :type file_handler:
            :py:class:`io.BufferedWriter`
        :param int bytes_remaining:
            The delta between the total file size in bytes and amount already
            downloaded.

        :rtype: None

        """
        file_handler.write(chunk)
        logger.debug(
            "download progress\n%s",
            pprint.pformat(
                {"chunk_size": len(chunk), "bytes_remaining": bytes_remaining,},
                indent=2,
            ),
        )
        on_progress = self._monostate.on_progress
        if on_progress:
            logger.debug("calling on_progress callback %s", on_progress)
            on_progress(self, chunk, file_handler, bytes_remaining)

    def on_complete(self, file_handle):
        """On download complete handler function.

        :param file_handle:
            The file handle where the media is being written to.
        :type file_handle:
            :py:class:`io.BufferedWriter`

        :rtype: None

        """
        logger.debug("download finished")
        on_complete = self._monostate.on_complete
        if on_complete:
            logger.debug("calling on_complete callback %s", on_complete)
            on_complete(self, file_handle)

    def __repr__(self) -> str:
        """Printable object representation.

        :rtype: str
        :returns:
            A string representation of a :class:`Stream <Stream>` object.
        """
        parts = ['itag="{s.itag}"', 'mime_type="{s.mime_type}"']
        if self.includes_video_track:
            parts.extend(['res="{s.resolution}"', 'fps="{s.fps}fps"'])
            if not self.is_adaptive:
                parts.extend(
                    ['vcodec="{s.video_codec}"', 'acodec="{s.audio_codec}"',]
                )
            else:
                parts.extend(['vcodec="{s.video_codec}"'])
        else:
            parts.extend(['abr="{s.abr}"', 'acodec="{s.audio_codec}"'])
        parts.extend(['progressive="{s.is_progressive}"', 'type="{s.type}"'])
        return f"<Stream: {' '.join(parts).format(s=self)}>"


class StreamQuery:
    """Interface for querying the available media streams."""

    def __init__(self, fmt_streams):
        """Construct a :class:`StreamQuery <StreamQuery>`.

        param list fmt_streams:
            list of :class:`Stream <Stream>` instances.
        """
        self.fmt_streams = fmt_streams
        self.itag_index = {int(s.itag): s for s in fmt_streams}

    def filter(
        self,
        fps=None,
        res=None,
        resolution=None,
        mime_type=None,
        type=None,
        subtype=None,
        file_extension=None,
        abr=None,
        bitrate=None,
        video_codec=None,
        audio_codec=None,
        only_audio=None,
        only_video=None,
        progressive=None,
        adaptive=None,
        is_dash=None,
        custom_filter_functions=None,
    ):
        """Apply the given filtering criterion.

        :param fps:
            (optional) The frames per second.
        :type fps:
            int or None

        :param resolution:
            (optional) Alias to ``res``.
        :type res:
            str or None

        :param res:
            (optional) The video resolution.
        :type resolution:
            str or None

        :param mime_type:
            (optional) Two-part identifier for file formats and format contents
            composed of a "type", a "subtype".
        :type mime_type:
            str or None

        :param type:
            (optional) Type part of the ``mime_type`` (e.g.: audio, video).
        :type type:
            str or None

        :param subtype:
            (optional) Sub-type part of the ``mime_type`` (e.g.: mp4, mov).
        :type subtype:
            str or None

        :param file_extension:
            (optional) Alias to ``sub_type``.
        :type file_extension:
            str or None

        :param abr:
            (optional) Average bitrate (ABR) refers to the average amount of
            data transferred per unit of time (e.g.: 64kbps, 192kbps).
        :type abr:
            str or None

        :param bitrate:
            (optional) Alias to ``abr``.
        :type bitrate:
            str or None

        :param video_codec:
            (optional) Video compression format.
        :type video_codec:
            str or None

        :param audio_codec:
            (optional) Audio compression format.
        :type audio_codec:
            str or None

        :param bool progressive:
            Excludes adaptive streams (one file contains both audio and video
            tracks).

        :param bool adaptive:
            Excludes progressive streams (audio and video are on separate
            tracks).

        :param bool is_dash:
            Include/exclude dash streams.

        :param bool only_audio:
            Excludes streams with video tracks.

        :param bool only_video:
            Excludes streams with audio tracks.

        :param custom_filter_functions:
            (optional) Interface for defining complex filters without
            subclassing.
        :type custom_filter_functions:
            list or None

        """
        filters = []
        if res or resolution:
            filters.append(lambda s: s.resolution == (res or resolution))

        if fps:
            filters.append(lambda s: s.fps == fps)

        if mime_type:
            filters.append(lambda s: s.mime_type == mime_type)

        if type:
            filters.append(lambda s: s.type == type)

        if subtype or file_extension:
            filters.append(lambda s: s.subtype == (subtype or file_extension))

        if abr or bitrate:
            filters.append(lambda s: s.abr == (abr or bitrate))

        if video_codec:
            filters.append(lambda s: s.video_codec == video_codec)

        if audio_codec:
            filters.append(lambda s: s.audio_codec == audio_codec)

        if only_audio:
            filters.append(
                lambda s: (s.includes_audio_track and not s.includes_video_track),
            )

        if only_video:
            filters.append(
                lambda s: (s.includes_video_track and not s.includes_audio_track),
            )

        if progressive:
            filters.append(lambda s: s.is_progressive)

        if adaptive:
            filters.append(lambda s: s.is_adaptive)

        if custom_filter_functions:
            for fn in custom_filter_functions:
                filters.append(fn)

        if is_dash is not None:
            filters.append(lambda s: s.is_dash == is_dash)

        fmt_streams = self.fmt_streams
        for fn in filters:
            fmt_streams = list(filter(fn, fmt_streams))
        return StreamQuery(fmt_streams)

    def order_by(self, attribute_name: str) -> "StreamQuery":
        """Apply a sort order. Filters out stream the do not have the attribute.

        :param str attribute_name:
            The name of the attribute to sort by.
        """
        has_attribute = [
            s for s in self.fmt_streams if getattr(s, attribute_name) is not None
        ]
        integer_attr_repr: Optional[Dict[str, int]] = None

        # check that the attribute value is a string
        if len(has_attribute) > 0 and isinstance(
            getattr(has_attribute[0], attribute_name), str
        ):
            # attempt to extract numerical values from string
            try:
                integer_attr_repr = {
                    getattr(s, attribute_name): int(
                        "".join(list(filter(str.isdigit, getattr(s, attribute_name))))
                    )
                    for s in has_attribute
                }
            except ValueError:
                integer_attr_repr = None

        # lookup integer values if we have them
        if integer_attr_repr is not None:
            return StreamQuery(
                sorted(
                    has_attribute,
                    key=lambda s: integer_attr_repr[getattr(s, attribute_name)],  # type: ignore  # noqa: E501
                )
            )

        return StreamQuery(
            sorted(has_attribute, key=lambda s: getattr(s, attribute_name))
        )

    def desc(self) -> "StreamQuery":
        """Sort streams in descending order.

        :rtype: :class:`StreamQuery <StreamQuery>`

        """
        return StreamQuery(self.fmt_streams[::-1])

    def asc(self) -> "StreamQuery":
        """Sort streams in ascending order.

        :rtype: :class:`StreamQuery <StreamQuery>`

        """
        return self

    def get_by_itag(self, itag: int) -> Optional[Stream]:
        """Get the corresponding :class:`Stream <Stream>` for a given itag.

        :param int itag:
            YouTube format identifier code.
        :rtype: :class:`Stream <Stream>` or None
        :returns:
            The :class:`Stream <Stream>` matching the given itag or None if
            not found.

        """
        return self.itag_index.get(int(itag))

    def get_by_resolution(self, resolution: str) -> Optional[Stream]:
        """Get the corresponding :class:`Stream <Stream>` for a given resolution.
        Stream must be a progressive mp4.

        :param str resolution:
            Video resolution i.e. "720p", "480p", "360p", "240p", "144p"
        :rtype: :class:`Stream <Stream>` or None
        :returns:
            The :class:`Stream <Stream>` matching the given itag or None if
            not found.

        """
        return self.filter(
            progressive=True, subtype="mp4", resolution=resolution
        ).first()

    def get_lowest_resolution(self) -> Optional[Stream]:
        """Get lowest resolution stream that is a progressive mp4.

        :rtype: :class:`Stream <Stream>` or None
        :returns:
            The :class:`Stream <Stream>` matching the given itag or None if
            not found.

        """
        return (
            self.filter(progressive=True, subtype="mp4")
            .order_by("resolution")
            .desc()
            .last()
        )

    def get_highest_resolution(self) -> Optional[Stream]:
        """Get highest resolution stream that is a progressive video.

        :rtype: :class:`Stream <Stream>` or None
        :returns:
            The :class:`Stream <Stream>` matching the given itag or None if
            not found.

        """
        return self.filter(progressive=True).order_by("resolution").asc().last()

    def get_audio_only(self, subtype: str = "mp4") -> Optional[Stream]:
        """Get highest bitrate audio stream for given codec (defaults to mp4)

        :param str subtype:
            Audio subtype, defaults to mp4
        :rtype: :class:`Stream <Stream>` or None
        :returns:
            The :class:`Stream <Stream>` matching the given itag or None if
            not found.

        """
        return (
            self.filter(only_audio=True, subtype=subtype).order_by("abr").asc().last()
        )

    def first(self) -> Optional[Stream]:
        """Get the first :class:`Stream <Stream>` in the results.

        :rtype: :class:`Stream <Stream>` or None
        :returns:
            the first result of this query or None if the result doesn't
            contain any streams.

        """
        try:
            return self.fmt_streams[0]
        except IndexError:
            return None

    def last(self):
        """Get the last :class:`Stream <Stream>` in the results.

        :rtype: :class:`Stream <Stream>` or None
        :returns:
            Return the last result of this query or None if the result
            doesn't contain any streams.

        """
        try:
            return self.fmt_streams[-1]
        except IndexError:
            pass

    def count(self) -> int:
        """Get the count the query would return.

        :rtype: int

        """
        return len(self.fmt_streams)

    def all(self) -> List[Stream]:
        """Get all the results represented by this query as a list.

        :rtype: list

        """
        return self.fmt_streams

def get_ytplayer_config(html: str, age_restricted: bool = False) -> Any:
    """Get the YouTube player configuration data from the watch html.

    Extract the ``ytplayer_config``, which is json data embedded within the
    watch html and serves as the primary source of obtaining the stream
    manifest data.

    :param str html:
        The html contents of the watch page.
    :param bool age_restricted:
        Is video age restricted.
    :rtype: str
    :returns:
        Substring of the html containing the encoded manifest data.
    """
    if age_restricted:
        pattern = r";yt\.setConfig\(\{'PLAYER_CONFIG':\s*({.*})(,'EXPERIMENT_FLAGS'|;)"  # noqa: E501
    else:
        pattern = r";ytplayer\.config\s*=\s*({.*?});"
    yt_player_config = regex_search(pattern, html, group=1)

    return json.loads(yt_player_config)

def regex_search(pattern: str, string: str, group: int) -> str:
    """Shortcut method to search a string for a given pattern.

    :param str pattern:
        A regular expression pattern.
    :param str string:
        A target string to search.
    :param int group:
        Index of group to return.
    :rtype:
        str or tuple
    :returns:
        Substring pattern matches.
    """
    regex = re.compile(pattern)
    results = regex.search(string)
    if not results:
        raise RegexMatchError(caller="regex_search", pattern=pattern)

    logger.debug(
        "finished regex search: %s",
        pprint.pformat({"pattern": pattern, "results": results.group(0),}, indent=2,),
    )

    return results.group(group)


class CaptionQuery:
    """Interface for querying the available captions."""

    def __init__(self, captions: List[Caption]):
        """Construct a :class:`Caption <Caption>`.

        param list captions:
            list of :class:`Caption <Caption>` instances.

        """
        self.captions = captions
        self.lang_code_index = {c.code: c for c in captions}

    def get_by_language_code(self, lang_code: str) -> Optional[Caption]:
        """Get the :class:`Caption <Caption>` for a given ``lang_code``.

        :param str lang_code:
            The code that identifies the caption language.
        :rtype: :class:`Caption <Caption>` or None
        :returns:
            The :class:`Caption <Caption>` matching the given ``lang_code`` or
            None if it does not exist.
        """
        return self.lang_code_index.get(lang_code)

    def all(self) -> List[Caption]:
        """Get all the results represented by this query as a list.

        :rtype: list

        """
        return self.captions


class PytubeHTMLParser(HTMLParser):
    in_vid_descr = False
    in_vid_descr_br = False
    vid_descr = ""

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            for attr in attrs:
                if attr[0] == "id" and attr[1] == "eow-description":
                    self.in_vid_descr = True

    def handle_endtag(self, tag):
        if self.in_vid_descr and tag == "p":
            self.in_vid_descr = False

    def handle_startendtag(self, tag, attrs):
        if self.in_vid_descr and tag == "br":
            self.in_vid_descr_br = True

    def handle_data(self, data):
        if self.in_vid_descr_br:
            self.vid_descr += f"\n{data}"
            self.in_vid_descr_br = False
        elif self.in_vid_descr:
            self.vid_descr += data

    def error(self, message):
        raise HTMLParseError(message)

def get_vid_descr(html: str) -> str:
    html_parser = PytubeHTMLParser()
    html_parser.feed(html)
    return html_parser.vid_descr


def video_info_url(
    video_id: str, watch_url: str, embed_html: Optional[str], age_restricted: bool,
) -> str:
    """Construct the video_info url.

    :param str video_id:
        A YouTube video identifier.
    :param str watch_url:
        A YouTube watch url.
    :param str embed_html:
        The html contents of the embed page (for age restricted videos).
    :param bool age_restricted:
        Is video age restricted.
    :rtype: str
    :returns:
        :samp:`https://youtube.com/get_video_info` with necessary GET
        parameters.
    """
    if age_restricted:
        assert embed_html is not None
        sts = regex_search(r'"sts"\s*:\s*(\d+)', embed_html, group=1)
        # Here we use ``OrderedDict`` so that the output is consistent between
        # Python 2.7+.
        params = OrderedDict(
            [("video_id", video_id), ("eurl", eurl(video_id)), ("sts", sts),]
        )
    else:
        params = OrderedDict(
            [
                ("video_id", video_id),
                ("el", "$el"),
                ("ps", "default"),
                ("eurl", quote(watch_url)),
                ("hl", "en_US"),
            ]
        )

    return "https://youtube.com/get_video_info?" + urlencode(params)


def js_url(html: str, age_restricted: Optional[bool] = False) -> str:
    """Get the base JavaScript url.

    Construct the base JavaScript url, which contains the decipher
    "transforms".

    :param str html:
        The html contents of the watch page.
    :param bool age_restricted:
        Is video age restricted.

    """
    ytplayer_config = get_ytplayer_config(html, age_restricted or False)
    base_js = ytplayer_config["assets"]["js"]
    return "https://youtube.com" + base_js

def video_id(url: str) -> str:
    """Extract the ``video_id`` from a YouTube url.

    This function supports the following patterns:

    - :samp:`https://youtube.com/watch?v={video_id}`
    - :samp:`https://youtube.com/embed/{video_id}`
    - :samp:`https://youtu.be/{video_id}`

    :param str url:
        A YouTube url containing a video id.
    :rtype: str
    :returns:
        YouTube video id.
    """
    return regex_search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url, group=1)

def is_age_restricted(watch_html: str) -> bool:
    """Check if content is age restricted.

    :param str watch_html:
        The html contents of the watch page.
    :rtype: bool
    :returns:
        Whether or not the content is age restricted.
    """
    try:
        regex_search(r"og:restrictions:age", watch_html, group=0)
    except RegexMatchError:
        return False
    return True

def watch_url(video_id: str) -> str:
    """Construct a sanitized YouTube watch url, given a video id.

    :param str video_id:
        A YouTube video identifier.
    :rtype: str
    :returns:
        Sanitized YouTube watch url.
    """
    return "https://youtube.com/watch?v=" + video_id


def embed_url(video_id: str) -> str:
    return f"https://www.youtube.com/embed/{video_id}"


def eurl(video_id: str) -> str:
    return f"https://youtube.googleapis.com/v/{video_id}"


def get_initial_function_name(js: str) -> str:
    """Extract the name of the function responsible for computing the signature.

    :param str js:
        The contents of the base.js asset file.
    :rtype: str
    :returns:
       Function name from regex match
    """

    function_patterns = [
        r"\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(",  # noqa: E501
        r"\b[a-zA-Z0-9]+\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(",  # noqa: E501
        r'\b(?P<sig>[a-zA-Z0-9$]{2})\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\)',  # noqa: E501
        r'(?P<sig>[a-zA-Z0-9$]+)\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\)',  # noqa: E501
        r'(["\'])signature\1\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(',
        r"\.sig\|\|(?P<sig>[a-zA-Z0-9$]+)\(",
        r"yt\.akamaized\.net/\)\s*\|\|\s*.*?\s*[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*(?:encodeURIComponent\s*\()?\s*(?P<sig>[a-zA-Z0-9$]+)\(",  # noqa: E501
        r"\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(",  # noqa: E501
        r"\b[a-zA-Z0-9]+\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(",  # noqa: E501
        r"\bc\s*&&\s*a\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(",  # noqa: E501
        r"\bc\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(",  # noqa: E501
        r"\bc\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(",  # noqa: E501
    ]

    logger.debug("finding initial function name")
    for pattern in function_patterns:
        regex = re.compile(pattern)
        results = regex.search(js)
        if results:
            logger.debug(f"finished regex search, matched: {pattern}")
            return results.group(1)

    raise RegexMatchError(caller="get_initial_function_name", pattern="multiple")


def get_transform_plan(js: str) -> List[str]:
    """Extract the "transform plan".

    The "transform plan" is the functions that the ciphered signature is
    cycled through to obtain the actual signature.

    :param str js:
        The contents of the base.js asset file.

    **Example**:

    >>> get_transform_plan(js)
    ['DE.AJ(a,15)',
    'DE.VR(a,3)',
    'DE.AJ(a,51)',
    'DE.VR(a,3)',
    'DE.kT(a,51)',
    'DE.kT(a,8)',
    'DE.VR(a,3)',
    'DE.kT(a,21)']
    """
    name = re.escape(get_initial_function_name(js))
    pattern = r"%s=function\(\w\){[a-z=\.\(\"\)]*;(.*);(?:.+)}" % name
    logger.debug("getting transform plan")
    return regex_search(pattern, js, group=1).split(";")


def get_transform_object(js: str, var: str) -> List[str]:
    """Extract the "transform object".

    The "transform object" contains the function definitions referenced in the
    "transform plan". The ``var`` argument is the obfuscated variable name
    which contains these functions, for example, given the function call
    ``DE.AJ(a,15)`` returned by the transform plan, "DE" would be the var.

    :param str js:
        The contents of the base.js asset file.
    :param str var:
        The obfuscated variable name that stores an object with all functions
        that descrambles the signature.

    **Example**:

    >>> get_transform_object(js, 'DE')
    ['AJ:function(a){a.reverse()}',
    'VR:function(a,b){a.splice(0,b)}',
    'kT:function(a,b){var c=a[0];a[0]=a[b%a.length];a[b]=c}']

    """
    pattern = r"var %s={(.*?)};" % re.escape(var)
    logger.debug("getting transform object")
    regex = re.compile(pattern, flags=re.DOTALL)
    results = regex.search(js)
    if not results:
        raise RegexMatchError(caller="get_transform_object", pattern=pattern)

    return results.group(1).replace("\n", " ").split(", ")


def get_transform_map(js: str, var: str) -> Dict:
    """Build a transform function lookup.

    Build a lookup table of obfuscated JavaScript function names to the
    Python equivalents.

    :param str js:
        The contents of the base.js asset file.
    :param str var:
        The obfuscated variable name that stores an object with all functions
        that descrambles the signature.

    """
    transform_object = get_transform_object(js, var)
    mapper = {}
    for obj in transform_object:
        # AJ:function(a){a.reverse()} => AJ, function(a){a.reverse()}
        name, function = obj.split(":", 1)
        fn = map_functions(function)
        mapper[name] = fn
    return mapper


def reverse(arr: List, _: Optional[Any]):
    """Reverse elements in a list.

    This function is equivalent to:

    .. code-block:: javascript

       function(a, b) { a.reverse() }

    This method takes an unused ``b`` variable as their transform functions
    universally sent two arguments.

    **Example**:

    >>> reverse([1, 2, 3, 4])
    [4, 3, 2, 1]
    """
    return arr[::-1]


def splice(arr: List, b: int):
    """Add/remove items to/from a list.

    This function is equivalent to:

    .. code-block:: javascript

       function(a, b) { a.splice(0, b) }

    **Example**:

    >>> splice([1, 2, 3, 4], 2)
    [1, 2]
    """
    return arr[:b] + arr[b * 2 :]


def swap(arr: List, b: int):
    """Swap positions at b modulus the list length.

    This function is equivalent to:

    .. code-block:: javascript

       function(a, b) { var c=a[0];a[0]=a[b%a.length];a[b]=c }

    **Example**:

    >>> swap([1, 2, 3, 4], 2)
    [3, 2, 1, 4]
    """
    r = b % len(arr)
    return list(chain([arr[r]], arr[1:r], [arr[0]], arr[r + 1 :]))


def map_functions(js_func: str) -> Callable:
    """For a given JavaScript transform function, return the Python equivalent.

    :param str js_func:
        The JavaScript version of the transform function.

    """
    mapper = (
        # function(a){a.reverse()}
        (r"{\w\.reverse\(\)}", reverse),
        # function(a,b){a.splice(0,b)}
        (r"{\w\.splice\(0,\w\)}", splice),
        # function(a,b){var c=a[0];a[0]=a[b%a.length];a[b]=c}
        (r"{var\s\w=\w\[0\];\w\[0\]=\w\[\w\%\w.length\];\w\[\w\]=\w}", swap),
        # function(a,b){var c=a[0];a[0]=a[b%a.length];a[b%a.length]=c}
        (
            r"{var\s\w=\w\[0\];\w\[0\]=\w\[\w\%\w.length\];\w\[\w\%\w.length\]=\w}",
            swap,
        ),
    )

    for pattern, fn in mapper:
        if re.search(pattern, js_func):
            return fn
    raise RegexMatchError(caller="map_functions", pattern="multiple")

def parse_function(js_func: str) -> Tuple[str, int]:
    """Parse the Javascript transform function.

    Break a JavaScript transform function down into a two element ``tuple``
    containing the function name and some integer-based argument.

    :param str js_func:
        The JavaScript version of the transform function.
    :rtype: tuple
    :returns:
        two element tuple containing the function name and an argument.

    **Example**:

    >>> parse_function('DE.AJ(a,15)')
    ('AJ', 15)

    """
    logger.debug("parsing transform function")
    pattern = r"\w+\.(\w+)\(\w,(\d+)\)"
    regex = re.compile(pattern)
    results = regex.search(js_func)
    if not results:
        raise RegexMatchError(caller="parse_function", pattern=pattern)
    fn_name, fn_arg = results.groups()
    return fn_name, int(fn_arg)

def get_signature(js: str, ciphered_signature: str) -> str:
    """Decipher the signature.

    Taking the ciphered signature, applies the transform functions.

    :param str js:
        The contents of the base.js asset file.
    :param str ciphered_signature:
        The ciphered signature sent in the ``player_config``.
    :rtype: str
    :returns:
       Decrypted signature required to download the media content.

    """
    transform_plan = get_transform_plan(js)
    var, _ = transform_plan[0].split(".")
    transform_map = get_transform_map(js, var)
    signature = [s for s in ciphered_signature]

    for js_func in transform_plan:
        name, argument = parse_function(js_func)
        signature = transform_map[name](signature, argument)
        logger.debug(
            "applied transform function\n"
            "output: %s\n"
            "js_function: %s\n"
            "argument: %d\n"
            "function: %s",
            "".join(signature),
            name,
            argument,
            transform_map[name],
        )

    return "".join(signature)

def apply_signature(config_args: Dict, fmt: str, js: str) -> None:
    """Apply the decrypted signature to the stream manifest.

    :param dict config_args:
        Details of the media streams available.
    :param str fmt:
        Key in stream manifests (``ytplayer_config``) containing progressive
        download or adaptive streams (e.g.: ``url_encoded_fmt_stream_map`` or
        ``adaptive_fmts``).
    :param str js:
        The contents of the base.js asset file.

    """
    stream_manifest = config_args[fmt]
    live_stream = (
        json.loads(config_args["player_response"])
        .get("playabilityStatus", {},)
        .get("liveStreamability")
    )
    for i, stream in enumerate(stream_manifest):
        try:
            url: str = stream["url"]
        except KeyError:
            if live_stream:
                raise LiveStreamError("Video is currently being streamed live")
        # 403 Forbidden fix.
        if "signature" in url or (
            "s" not in stream and ("&sig=" in url or "&lsig=" in url)
        ):
            # For certain videos, YouTube will just provide them pre-signed, in
            # which case there's no real magic to download them and we can skip
            # the whole signature descrambling entirely.
            logger.debug("signature found, skip decipher")
            continue

        if js is not None:
            signature = get_signature(js, stream["s"])
        else:
            # signature not present in url (line 33), need js to descramble
            # TypeError caught in __main__
            raise TypeError("JS is None")

        logger.debug(
            "finished descrambling signature for itag=%s\n%s",
            stream["itag"],
            pprint.pformat({"s": stream["s"], "signature": signature,}, indent=2,),
        )
        # 403 forbidden fix
        stream_manifest[i]["url"] = url + "&sig=" + signature


def apply_descrambler(stream_data: Dict, key: str) -> None:
    """Apply various in-place transforms to YouTube's media stream data.

    Creates a ``list`` of dictionaries by string splitting on commas, then
    taking each list item, parsing it as a query string, converting it to a
    ``dict`` and unquoting the value.

    :param dict stream_data:
        Dictionary containing query string encoded values.
    :param str key:
        Name of the key in dictionary.

    **Example**:

    >>> d = {'foo': 'bar=1&var=test,em=5&t=url%20encoded'}
    >>> apply_descrambler(d, 'foo')
    >>> print(d)
    {'foo': [{'bar': '1', 'var': 'test'}, {'em': '5', 't': 'url encoded'}]}

    """
    if key == "url_encoded_fmt_stream_map" and not stream_data.get(
        "url_encoded_fmt_stream_map"
    ):
        formats = json.loads(stream_data["player_response"])["streamingData"]["formats"]
        formats.extend(json.loads(stream_data["player_response"])["streamingData"]["adaptiveFormats"])
        try:
            stream_data[key] = [
                {
                    "url": format_item["url"],
                    "type": format_item["mimeType"],
                    "quality": format_item["quality"],
                    "itag": format_item["itag"],
                }
                for format_item in formats
            ]
        except KeyError:
            cipher_url = [
                parse_qs(formats[i]["cipher"]) for i, data in enumerate(formats)
            ]
            stream_data[key] = [
                {
                    "url": cipher_url[i]["url"][0],
                    "s": cipher_url[i]["s"][0],
                    "type": format_item["mimeType"],
                    "quality": format_item["quality"],
                    "itag": format_item["itag"],
                }
                for i, format_item in enumerate(formats)
            ]
    else:
        stream_data[key] = [
            {k: unquote(v) for k, v in parse_qsl(i)}
            for i in stream_data[key].split(",")
        ]
    logger.debug(
        "applying descrambler\n%s", pprint.pformat(stream_data[key], indent=2),
    )

class YouTube:
    """Core developer interface for pytube."""

    def __init__(
        self,
        url: str,
        defer_prefetch_init: bool = False,
        proxies: Dict[str, str] = None,
    ):
        """Construct a :class:`YouTube <YouTube>`.

        :param str url:
            A valid YouTube watch URL.
        :param bool defer_prefetch_init:
            Defers executing any network requests.
        :param func on_progress_callback:
            (Optional) User defined callback function for stream download
            progress events.
        :param func on_complete_callback:
            (Optional) User defined callback function for stream download
            complete events.

        """
        self.js: Optional[str] = None  # js fetched by js_url
        self.js_url: Optional[str] = None  # the url to the js, parsed from watch html

        # note: vid_info may eventually be removed. It sounds like it once had
        # additional formats, but that doesn't appear to still be the case.

        # the url to vid info, parsed from watch html
        self.vid_info_url: Optional[str] = None
        self.vid_info_raw: Optional[str] = None  # content fetched by vid_info_url
        self.vid_info: Optional[Dict] = None  # parsed content of vid_info_raw

        self.watch_html: Optional[str] = None  # the html of /watch?v=<video_id>
        self.embed_html: Optional[str] = None
        self.player_config_args: Dict = {}  # inline js in the html containing streams

        self.age_restricted: Optional[bool] = None
        self.vid_descr: Optional[str] = None

        self.fmt_streams: List[Stream] = []
        self.caption_tracks: List[Caption] = []

        # video_id part of /watch?v=<video_id>
        self.video_id = video_id(url)

        # https://www.youtube.com/watch?v=<video_id>
        self.watch_url = watch_url(self.video_id)

        self.embed_url = embed_url(self.video_id)
        # A dictionary shared between all instances of :class:`Stream <Stream>`
        # (Borg pattern). Boooooo.
        self.stream_monostate = Monostate()

        if not defer_prefetch_init:
            self.prefetch()
            self.descramble()

    def descramble(self) -> None:
        """Descramble the stream data and build Stream instances.

        The initialization process takes advantage of Python's
        "call-by-reference evaluation," which allows dictionary transforms to
        be applied in-place, instead of holding references to mutations at each
        interstitial step.

        :rtype: None

        """
        logger.info("init started")

        self.vid_info = dict(parse_qsl(self.vid_info_raw))

        pyperclip.copy(json.dumps(self.vid_info))
        print("NICE")

        # Check to see if you need to make this distinction
        if self.age_restricted:
            self.player_config_args = self.vid_info
        else:
            assert self.watch_html is not None
            self.player_config_args = get_ytplayer_config(self.watch_html,)[
                "args"
            ]

            # Fix for KeyError: 'title' issue #434
            if "title" not in self.player_config_args:  # type: ignore
                i_start = self.watch_html.lower().index("<title>") + len("<title>")
                i_end = self.watch_html.lower().index("</title>")
                title = self.watch_html[i_start:i_end].strip()
                index = title.lower().rfind(" - youtube")
                title = title[:index] if index > 0 else title
                self.player_config_args["title"] = unescape(title)

        if self.watch_html:
            self.vid_descr = get_vid_descr(self.watch_html)
        # https://github.com/nficano/pytube/issues/165
        stream_maps = ["url_encoded_fmt_stream_map"]
        if "adaptive_fmts" in self.player_config_args:
            stream_maps.append("adaptive_fmts")

        # unscramble the progressive and adaptive stream manifests.
        for fmt in stream_maps:
            if not self.age_restricted and fmt in self.vid_info:
                print("YES")
                apply_descrambler(self.vid_info, fmt)

            apply_descrambler(self.player_config_args, fmt)

            try:
                apply_signature(
                    self.player_config_args, fmt, self.js  # type: ignore
                )
            except TypeError:
                print(fmt)
                assert self.embed_html is not None
                self.js_url = js_url(self.embed_html, self.age_restricted)
                self.js = get(self.js_url)
                assert self.js is not None
                apply_signature(self.player_config_args, fmt, self.js)

            # build instances of :class:`Stream <Stream>`
            self.initialize_stream_objects(fmt)

        # load the player_response object (contains subtitle information)
        self.player_config_args["player_response"] = json.loads(
            self.player_config_args["player_response"]
        )

        self.initialize_caption_objects()
        logger.info("init finished successfully")

    def prefetch(self) -> None:
        """Eagerly download all necessary data.

        Eagerly executes all necessary network requests so all other
        operations don't does need to make calls outside of the interpreter
        which blocks for long periods of time.

        :rtype: None

        """
        self.watch_html = get(url=self.watch_url)
        if (
            self.watch_html is None
            or '<img class="icon meh" src="/yts/img'  # noqa: W503
            not in self.watch_html  # noqa: W503
        ):
            raise VideoUnavailable(video_id=self.video_id)

        self.embed_html = get(url=self.embed_url)
        self.age_restricted = is_age_restricted(self.watch_html)
        self.vid_info_url = video_info_url(
            video_id=self.video_id,
            watch_url=self.watch_url,
            embed_html=self.embed_html,
            age_restricted=self.age_restricted,
        )
        self.vid_info_raw = get(self.vid_info_url)

        if not self.age_restricted:
            self.js_url = js_url(self.watch_html, self.age_restricted)
            self.js = get(self.js_url)

    def initialize_stream_objects(self, fmt: str) -> None:
        """Convert manifest data to instances of :class:`Stream <Stream>`.

        Take the unscrambled stream data and uses it to initialize
        instances of :class:`Stream <Stream>` for each media stream.

        :param str fmt:
            Key in stream manifest (``ytplayer_config``) containing progressive
            download or adaptive streams (e.g.: ``url_encoded_fmt_stream_map``
            or ``adaptive_fmts``).

        :rtype: None

        """
        stream_manifest = self.player_config_args[fmt]
        for stream in stream_manifest:
            video = Stream(
                stream=stream,
                player_config_args=self.player_config_args,
                monostate=self.stream_monostate,
            )
            self.fmt_streams.append(video)

    def initialize_caption_objects(self) -> None:
        """Populate instances of :class:`Caption <Caption>`.

        Take the unscrambled player response data, and use it to initialize
        instances of :class:`Caption <Caption>`.

        :rtype: None

        """
        if "captions" not in self.player_config_args["player_response"]:
            return
        # https://github.com/nficano/pytube/issues/167
        caption_tracks = (
            self.player_config_args.get("player_response", {})
            .get("captions", {})
            .get("playerCaptionsTracklistRenderer", {})
            .get("captionTracks", [])
        )
        for caption_track in caption_tracks:
            self.caption_tracks.append(Caption(caption_track))

    @property
    def captions(self) -> CaptionQuery:
        """Interface to query caption tracks.

        :rtype: :class:`CaptionQuery <CaptionQuery>`.
        """
        return CaptionQuery(self.caption_tracks)

    @property
    def streams(self) -> StreamQuery:
        """Interface to query both adaptive (DASH) and progressive streams.

        :rtype: :class:`StreamQuery <StreamQuery>`.
        """
        return StreamQuery(self.fmt_streams)

    @property
    def thumbnail_url(self) -> str:
        """Get the thumbnail url image.

        :rtype: str

        """
        player_response = self.player_config_args.get("player_response", {})
        thumbnail_details = (
            player_response.get("videoDetails", {})
            .get("thumbnail", {})
            .get("thumbnails")
        )
        if thumbnail_details:
            thumbnail_details = thumbnail_details[-1]  # last item has max size
            return thumbnail_details["url"]

        return "https://img.youtube.com/vi/" + self.video_id + "/maxresdefault.jpg"

    @property
    def title(self) -> str:
        """Get the video title.

        :rtype: str

        """
        return self.player_config_args.get("title") or (
            self.player_config_args.get("player_response", {})
            .get("videoDetails", {})
            .get("title")
        )

    @property
    def description(self) -> str:
        """Get the video description.

        :rtype: str

        """
        return self.vid_descr or (
            self.player_config_args.get("player_response", {})
            .get("videoDetails", {})
            .get("shortDescription")
        )

    @property
    def rating(self) -> float:
        """Get the video average rating.

        :rtype: float

        """
        return (
            self.player_config_args.get("player_response", {})
            .get("videoDetails", {})
            .get("averageRating")
        )

    @property
    def length(self) -> str:
        """Get the video length in seconds.

        :rtype: str

        """
        return self.player_config_args.get("length_seconds") or (
            self.player_config_args.get("player_response", {})
            .get("videoDetails", {})
            .get("lengthSeconds")
        )

    @property
    def views(self) -> str:
        """Get the number of the times the video has been viewed.

        :rtype: str

        """
        return (
            self.player_config_args.get("player_response", {})
            .get("videoDetails", {})
            .get("viewCount")
        )

    @property
    def author(self) -> str:
        """Get the video author.
        :rtype: str
        """
        return (
            self.player_config_args.get("player_response", {})
            .get("videoDetails", {})
            .get("author", "unknown")
        )

    def register_on_progress_callback(self, func):
        """Register a download progress callback function post initialization.

        :param callable func:
            A callback function that takes ``stream``, ``chunk``,
            ``file_handle``, ``bytes_remaining`` as parameters.

        :rtype: None

        """
        self.stream_monostate.on_progress = func

    def register_on_complete_callback(self, func):
        """Register a download complete callback function post initialization.

        :param callable func:
            A callback function that takes ``stream`` and  ``file_handle``.

        :rtype: None

        """
        self.stream_monostate.on_complete = func

def mime_type_codec(mime_type_codec: str) -> Tuple[str, List[str]]:
    """Parse the type data.

    Breaks up the data in the ``type`` key of the manifest, which contains the
    mime type and codecs serialized together, and splits them into separate
    elements.

    **Example**:

    mime_type_codec('audio/webm; codecs="opus"') -> ('audio/webm', ['opus'])

    :param str mime_type_codec:
        String containing mime type and codecs.
    :rtype: tuple
    :returns:
        The mime type and a list of codecs.

    """
    pattern = r"(\w+\/\w+)\;\scodecs=\"([a-zA-Z-0-9.,\s]*)\""
    regex = re.compile(pattern)
    results = regex.search(mime_type_codec)
    if not results:
        raise RegexMatchError(caller="mime_type_codec", pattern=pattern)
    mime_type, codecs = results.groups()
    return mime_type, [c.strip() for c in codecs.split(",")]


def target_directory(output_path: Optional[str] = None) -> str:
    """
    Function for determining target directory of a download.
    Returns an absolute path (if relative one given) or the current
    path (if none given). Makes directory if it does not exist.

    :type output_path: str
        :rtype: str
    :returns:
        An absolute directory path as a string.
    """
    if output_path:
        if not os.path.isabs(output_path):
            output_path = os.path.join(os.getcwd(), output_path)
    else:
        output_path = os.getcwd()
    os.makedirs(output_path, exist_ok=True)
    return output_path

def safe_filename(s: str, max_length: int = 255) -> str:
    """Sanitize a string making it safe to use as a filename.

    This function was based off the limitations outlined here:
    https://en.wikipedia.org/wiki/Filename.

    :param str s:
        A string to make safe for use as a file name.
    :param int max_length:
        The maximum filename character length.
    :rtype: str
    :returns:
        A sanitized string.
    """
    # Characters in range 0-31 (0x00-0x1F) are not allowed in ntfs filenames.
    ntfs_characters = [chr(i) for i in range(0, 31)]
    characters = [
        r'"',
        r"\#",
        r"\$",
        r"\%",
        r"'",
        r"\*",
        r"\,",
        r"\.",
        r"\/",
        r"\:",
        r'"',
        r"\;",
        r"\<",
        r"\>",
        r"\?",
        r"\\",
        r"\^",
        r"\|",
        r"\~",
        r"\\\\",
    ]
    pattern = "|".join(ntfs_characters + characters)
    regex = re.compile(pattern, re.UNICODE)
    filename = regex.sub("", s)
    return filename[:max_length].rsplit(" ", 0)[0]

def _execute_request(url: str) -> Any:
    if not url.lower().startswith("http"):
        raise ValueError
    return urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}))  # nosec


def get(url) -> str:
    """Send an http GET request.

    :param str url:
        The URL to perform the GET request for.
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    return _execute_request(url).read().decode("utf-8")


def stream(url: str, chunk_size: int = 8192) -> Iterable[bytes]:
    """Read the response in chunks.
    :param str url:
        The URL to perform the GET request for.
    :param int chunk_size:
        The size in bytes of each chunk. Defaults to 8*1024
    :rtype: Iterable[bytes]
    """
    response = _execute_request(url)
    while True:
        buf = response.read(chunk_size)
        if not buf:
            break
        yield buf

class PytubeError(Exception):
    """Base pytube exception that all others inherent.

    This is done to not pollute the built-in exceptions, which *could* result
    in unintended errors being unexpectedly and incorrectly handled within
    implementers code.
    """


class ExtractError(PytubeError):
    """Data extraction based exception."""


class RegexMatchError(ExtractError):
    """Regex pattern did not return any matches."""

    def __init__(self, caller: str, pattern: Union[str, Pattern]):
        """
        :param str caller:
            Calling function
        :param str pattern:
            Pattern that failed to match
        """
        super().__init__(f"{caller}: could not find match for {pattern}")
        self.caller = caller
        self.pattern = pattern


class LiveStreamError(ExtractError):
    """Video is a live stream."""


class VideoUnavailable(PytubeError):
    """Video is unavailable."""

    def __init__(self, video_id: str):
        """
        :param str video_id:
            A YouTube video identifier.
        """
        super().__init__(f"{video_id} is unavailable")

        self.video_id = video_id


class HTMLParseError(PytubeError):
    """HTML could not be parsed"""

ITAGS = {
    5: ("240p", "64kbps"),
    6: ("270p", "64kbps"),
    13: ("144p", None),
    17: ("144p", "24kbps"),
    18: ("360p", "96kbps"),
    22: ("720p", "192kbps"),
    34: ("360p", "128kbps"),
    35: ("480p", "128kbps"),
    36: ("240p", None),
    37: ("1080p", "192kbps"),
    38: ("3072p", "192kbps"),
    43: ("360p", "128kbps"),
    44: ("480p", "128kbps"),
    45: ("720p", "192kbps"),
    46: ("1080p", "192kbps"),
    59: ("480p", "128kbps"),
    78: ("480p", "128kbps"),
    82: ("360p", "128kbps"),
    83: ("480p", "128kbps"),
    84: ("720p", "192kbps"),
    85: ("1080p", "192kbps"),
    91: ("144p", "48kbps"),
    92: ("240p", "48kbps"),
    93: ("360p", "128kbps"),
    94: ("480p", "128kbps"),
    95: ("720p", "256kbps"),
    96: ("1080p", "256kbps"),
    100: ("360p", "128kbps"),
    101: ("480p", "192kbps"),
    102: ("720p", "192kbps"),
    132: ("240p", "48kbps"),
    151: ("720p", "24kbps"),
    # DASH Video
    133: ("240p", None),
    134: ("360p", None),
    135: ("480p", None),
    136: ("720p", None),
    137: ("1080p", None),
    138: ("2160p", None),
    160: ("144p", None),
    167: ("360p", None),
    168: ("480p", None),
    169: ("720p", None),
    170: ("1080p", None),
    212: ("480p", None),
    218: ("480p", None),
    219: ("480p", None),
    242: ("240p", None),
    243: ("360p", None),
    244: ("480p", None),
    245: ("480p", None),
    246: ("480p", None),
    247: ("720p", None),
    248: ("1080p", None),
    264: ("1440p", None),
    266: ("2160p", None),
    271: ("1440p", None),
    272: ("2160p", None),
    278: ("144p", None),
    298: ("720p", None),
    299: ("1080p", None),
    302: ("720p", None),
    303: ("1080p", None),
    308: ("1440p", None),
    313: ("2160p", None),
    315: ("2160p", None),
    330: ("144p", None),
    331: ("240p", None),
    332: ("360p", None),
    333: ("480p", None),
    334: ("720p", None),
    335: ("1080p", None),
    336: ("1440p", None),
    337: ("2160p", None),
    # DASH Audio
    139: (None, "48kbps"),
    140: (None, "128kbps"),
    141: (None, "256kbps"),
    171: (None, "128kbps"),
    172: (None, "256kbps"),
    249: (None, "50kbps"),
    250: (None, "70kbps"),
    251: (None, "160kbps"),
    256: (None, None),
    258: (None, None),
    325: (None, None),
    328: (None, None),
}

HDR = [330, 331, 332, 333, 334, 335, 336, 337]
_60FPS = [298, 299, 302, 303, 308, 315] + HDR
_3D = [82, 83, 84, 85, 100, 101, 102]
LIVE = [91, 92, 93, 94, 95, 96, 132, 151]
DASH_MP4_VIDEO = [133, 134, 135, 136, 137, 138, 160, 212, 264, 266, 298, 299]
DASH_MP4_AUDIO = [139, 140, 141, 256, 258, 325, 328]
DASH_WEBM_VIDEO = [
    167,
    168,
    169,
    170,
    218,
    219,
    278,
    242,
    243,
    244,
    245,
    246,
    247,
    248,
    271,
    272,
    302,
    303,
    308,
    313,
    315,
]
DASH_WEBM_AUDIO = [171, 172, 249, 250, 251]

def get_format_profile(itag: int) -> Dict:
    """Get additional format information for a given itag.

    :param str itag:
        YouTube format identifier code.
    """
    itag = int(itag)
    if itag in ITAGS:
        res, bitrate = ITAGS[itag]
    else:
        res, bitrate = None, None
    return {
        "resolution": res,
        "abr": bitrate,
        "is_live": itag in LIVE,
        "is_3d": itag in _3D,
        "is_hdr": itag in HDR,
        "fps": 60 if itag in _60FPS else 30,
        "is_dash": itag in DASH_MP4_VIDEO
        or itag in DASH_MP4_AUDIO
        or itag in DASH_WEBM_VIDEO
        or itag in DASH_WEBM_AUDIO,
    }



def main():
    vod = YouTube("https://www.youtube.com/watch?v=DBzuYNK95sM")

    myVideoStreams = vod.streams

    audioStream = myVideoStreams.filter(type = "audio", audio_codec="mp4a.40.2")

    best_stream = None
    hq = 0

    for stream in audioStream.all():
        print(stream.parse_codecs())

        if stream.filesize > hq:
            hq = stream.filesize
            best_stream = stream

    best_stream.download(output_path="./", filename="ratmtest")

if __name__ == "__main__":
    main()