"""ListItem construction.

Two Kodi behaviours drive everything here.

First, `ART::FillInDefaultIcon` only supplies a default when the `icon` art key
is empty, and Estuary's view templates fall through to a `DefaultVideo.png`
fallback texture. Every item therefore sets `icon`, `thumb` and `poster`.

Second, `VIDEO::IsVideo` returns true the moment an item has a video info tag,
which then drags the item onto the video default icon and the video info
dialog. So `getVideoInfoTag()` is never called on a still, only on a video.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import xbmc
import xbmcgui

from api import Asset
from kodiutils import ADDON_FANART, media

# Read once per interpreter, not once per invocation: with
# <reuselanguageinvoker> the module survives between navigations. Changing the
# Kodi region therefore needs a restart before labels follow. Accepted, because
# re-reading these on every listing costs two JSON-RPC-backed lookups per row.
_DATE_LONG = xbmc.getRegion("datelong")
_TIME = xbmc.getRegion("time")


def _strftime(value: datetime, fmt: str) -> str:
    """Format a Kodi region format without relying on libc's `%-d` / `%-m`.

    Kodi emits those no-pad directives for a one-character day or month.
    Some libc implementations reject them; others copy them as `-d` or `-m`
    without raising. Render portable zero-padded values separately and insert
    them through literal sentinels instead.
    """
    replacements = (
        ("%-d", "__IMMICH_DAY__", value.strftime("%d").lstrip("0") or "0"),
        ("%-m", "__IMMICH_MONTH__", value.strftime("%m").lstrip("0") or "0"),
    )
    for token, marker, replacement in replacements:
        if token in fmt:
            fmt = fmt.replace(token, marker)

    rendered = value.strftime(fmt)
    for _token, marker, replacement in replacements:
        rendered = rendered.replace(marker, replacement)
    return rendered


def format_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return _strftime(value, f"{_DATE_LONG} {_TIME}")


def format_month(value: datetime) -> str:
    """Month and year, for a timeline bucket label.

    Not built by stripping the day out of the regional long-date format, which
    leaves stray separators behind ("June , 2025"). Not built from `%B` either:
    that resolves against the C locale, which LibreELEC does not populate, so
    every label would be English whatever the Kodi language is. Kodi's own core
    strings 21-32 are the translated month names.
    """
    name = xbmc.getLocalizedString(20 + value.month) or value.strftime("%B")
    return f"{name} {value.year}"


def _w3c(value: Optional[datetime]) -> str:
    """W3C datetime, which is what setDateTime and setDateTimeTaken expect."""
    return value.strftime("%Y-%m-%dT%H:%M:%S") if value else ""


def _kodi_datetime(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def menu_item(
    label: str, icon_name: str, description: str = "", is_folder: bool = True
) -> xbmcgui.ListItem:
    """A root or section entry.

    `offscreen=True` skips GUI locking on every setter, which matters because a
    listing builds hundreds of these. `is_folder` must match the flag passed
    alongside the item to `addDirectoryItems`, or the two disagree.
    """
    item = xbmcgui.ListItem(label=label, offscreen=True)
    art = media(f"{icon_name}.png")
    item.setArt(
        {
            "icon": art,
            "thumb": art,
            "poster": art,
            "fanart": ADDON_FANART,
        }
    )
    if description:
        item.setProperty("description", description)
        item.setProperty("plot", description)
    item.setIsFolder(is_folder)
    return item


def folder_item(
    label: str,
    thumb: Optional[str] = None,
    icon_name: str = "albums",
    date: Optional[datetime] = None,
    label2: str = "",
) -> xbmcgui.ListItem:
    """A folder backed by remote artwork, such as an album or a person.

    The bundled icon stays in the `icon` slot so the item never falls back to a
    Kodi default while the remote thumbnail is still being cached.
    """
    item = xbmcgui.ListItem(label=label, label2=label2, offscreen=True)
    fallback = media(f"{icon_name}.png")
    # A 250px thumbnail stretched to a full-screen backdrop looks worse than the
    # addon fanart, so folders keep the fanart and only use the thumbnail up
    # front.
    item.setArt(
        {
            "icon": fallback,
            "thumb": thumb or fallback,
            "poster": thumb or fallback,
            "fanart": ADDON_FANART,
        }
    )
    if date is not None:
        item.setDateTime(_w3c(date))
    item.setIsFolder(True)
    return item


def _asset_art(thumb: str, fanart: str, icon_name: str) -> dict:
    """Art for a photo or video row.

    `icon` stays a bundled file rather than the remote thumbnail. Kodi's skin
    falls back to DefaultVideo.png whenever the art it is asked for has not
    resolved — a cold texture cache, a thumbnail job that has not run, a 401
    after a key rotation — so pointing `icon` at the network reintroduces the
    generic-icon bug on the largest listings. `thumb` and `poster` carry the
    real picture; `icon` is the floor beneath them.
    """
    fallback = media(f"{icon_name}.png")
    return {
        "icon": fallback,
        "thumb": thumb or fallback,
        "poster": thumb or fallback,
        "fanart": fanart or thumb or ADDON_FANART,
    }


def _describe(asset: Asset) -> str:
    """Human-readable metadata block.

    InfoTagPicture can only hold resolution and date-taken, and
    `setInfo('pictures', ...)` silently discards every other EXIF field, so
    camera details are surfaced as text and as properties instead.
    """
    lines = []
    if asset.description:
        lines.append(asset.description)
    where = ", ".join(part for part in (asset.city, asset.country) if part)
    if where:
        lines.append(where)
    if asset.taken_at:
        lines.append(format_datetime(asset.taken_at))
    exif = asset.exif or {}
    camera = " ".join(part for part in (exif.get("make"), exif.get("model")) if part)
    if camera:
        lines.append(camera)
    settings = []
    if exif.get("fNumber"):
        settings.append(f"f/{exif['fNumber']}")
    if exif.get("exposureTime"):
        settings.append(f"{exif['exposureTime']}s")
    if exif.get("iso"):
        settings.append(f"ISO {exif['iso']}")
    if exif.get("focalLength"):
        settings.append(f"{round(float(exif['focalLength']))}mm")
    if settings:
        lines.append("  ".join(settings))
    if asset.people:
        lines.append(", ".join(asset.people))
    return "\n".join(lines)


def _apply_properties(item: xbmcgui.ListItem, asset: Asset):
    """Expose metadata skins can read as ListItem.Property(name)."""
    properties = {"immich_id": asset.id}
    if asset.city:
        properties["immich_city"] = asset.city
    if asset.country:
        properties["immich_country"] = asset.country
    exif = asset.exif or {}
    for source, target in (
        ("make", "immich_camera_make"),
        ("model", "immich_camera_model"),
        ("lensModel", "immich_lens"),
        ("iso", "immich_iso"),
        ("fNumber", "immich_aperture"),
        ("exposureTime", "immich_exposure"),
        ("focalLength", "immich_focal_length"),
        ("latitude", "immich_latitude"),
        ("longitude", "immich_longitude"),
    ):
        if exif.get(source) is not None:
            properties[target] = str(exif[source])
    if asset.is_favorite:
        properties["immich_favorite"] = "true"
    item.setProperties(properties)


def photo_item(
    asset: Asset, label: str, url: str, thumb: str, fanart: str = ""
) -> xbmcgui.ListItem:
    """A still.

    Deliberately never touches getVideoInfoTag: creating a video info tag makes
    Kodi classify the item as video, which changes both its default icon and
    the info dialog it opens.
    """
    where = ", ".join(part for part in (asset.city, asset.country) if part)
    item = xbmcgui.ListItem(label=label, label2=where, offscreen=True)
    item.setArt(_asset_art(thumb, fanart, "photos"))

    picture = item.getPictureInfoTag()
    if asset.width and asset.height:
        picture.setResolution(int(asset.width), int(asset.height))
    if asset.taken_at:
        picture.setDateTimeTaken(_w3c(asset.taken_at))
        item.setDateTime(_w3c(asset.taken_at))

    _apply_properties(item, asset)
    item.setProperty("plot", _describe(asset))
    # A still whose container mimetype is video/* would be reclassified as
    # video by IsVideo, which is the same trap getVideoInfoTag sets. Motion
    # photos and Live Photo containers hit this.
    if asset.mime_type and not asset.mime_type.startswith("video/"):
        item.setMimeType(asset.mime_type)
        item.setContentLookup(False)
    item.setPath(url)
    return item


def video_item(
    asset: Asset, label: str, url: str, thumb: str, fanart: str = ""
) -> xbmcgui.ListItem:
    item = xbmcgui.ListItem(label=label, offscreen=True)
    item.setArt(_asset_art(thumb, fanart, "videos"))

    tag = item.getVideoInfoTag()
    tag.setMediaType("video")
    tag.setTitle(label)
    tag.setPlot(_describe(asset))
    if asset.duration:
        tag.setDuration(int(asset.duration))
    if asset.taken_at:
        tag.setPremiered(asset.taken_at.strftime("%Y-%m-%d"))
        tag.setDateAdded(_kodi_datetime(asset.taken_at))
        item.setDateTime(_w3c(asset.taken_at))
    if asset.city or asset.country:
        tag.setCountries([part for part in (asset.city, asset.country) if part])

    _apply_properties(item, asset)
    # Disabling content lookup suppresses the HEAD probe, and the mimetype is
    # what replaces it. Timeline assets carry no mimetype (the columnar bucket
    # does not include one) and the playback URL has no file extension, so
    # disabling the probe there left Kodi with no way to identify the stream.
    if asset.mime_type:
        item.setMimeType(asset.mime_type)
        item.setContentLookup(False)
    item.setProperty("IsPlayable", "true")
    item.setPath(url)
    return item


def asset_label(asset: Asset, name_mode: int) -> str:
    """Label an asset by date or by original filename.

    Timeline buckets do not carry filenames, so a filename preference falls back
    to the date rather than showing a blank row.
    """
    if name_mode == 1 and asset.filename:
        return asset.filename
    if asset.taken_at:
        return format_datetime(asset.taken_at)
    return asset.filename or asset.id
