"""The test cases.

Each case takes the Harness and returns a list of problem strings. An empty
list is a pass. Cases are registered with @case and run in declaration order.
"""

from __future__ import annotations

import ast
import glob
import os
import re
import subprocess
import sys
import zipfile
from xml.etree import ElementTree
from urllib.parse import parse_qsl, unquote, urlparse

import harness
import xbmc
from harness import Harness, check_kodi_url, discover_routes, standard_checks
from kodi_state import SETTINGS_SCHEMA, STRINGS
from mockimmich import API_KEY, _uuid, parse_version

REPO = harness.REPO
LIB = os.path.join(REPO, "resources", "lib")
MEDIA_DIR = os.path.join(REPO, "resources", "media")
STUBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kodistubs")

CASES = []
COVERED = set()


def case(name, route=None):
    def register(function):
        CASES.append((name, function, route))
        return function

    return register


ALBUM_1 = _uuid("b", 1)
ALBUM_2 = _uuid("b", 2)
MEMORY_1 = _uuid("m", 1)
PERSON_1 = _uuid("p", 1)
TAG_1 = _uuid("t", 1)


# ==========================================================================
# Static consistency
# ==========================================================================


def _lib_sources():
    return sorted(glob.glob(os.path.join(LIB, "*.py")) + [os.path.join(REPO, "addon.py")])


@case("static: every localise()/error_dialog() id exists in strings.po")
def static_string_ids(h):
    problems = []
    for path in _lib_sources():
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ("localise", "error_dialog"):
                continue
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, int):
                    if argument.value not in STRINGS:
                        problems.append(
                            f"{path}:{node.lineno}: {node.func.id}"
                            f"({argument.value}) has no msgid in strings.po"
                        )
    return problems


@case("static: every label/help id in settings.xml exists in strings.po")
def static_settings_labels(h):
    problems = []
    path = os.path.join(REPO, "resources", "settings.xml")
    text = open(path, encoding="utf-8").read()
    for match in re.finditer(r'(label|help|heading)>?="?(\d{5})', text):
        if int(match.group(2)) not in STRINGS:
            line = text[: match.start()].count("\n") + 1
            problems.append(
                f"{path}:{line}: {match.group(1)}={match.group(2)} has no "
                f"msgid in strings.po"
            )
    for match in re.finditer(r"<heading>(\d{5})</heading>", text):
        if int(match.group(1)) not in STRINGS:
            problems.append(f"{path}: heading {match.group(1)} missing from strings.po")
    return problems


@case("static: every setting id the code reads is declared in settings.xml")
def static_setting_ids(h):
    problems = []
    path = os.path.join(LIB, "kodiutils.py")
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("_string", "_bool", "_int")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            key = node.args[0].value
            if key not in SETTINGS_SCHEMA:
                problems.append(
                    f"{path}:{node.lineno}: reads setting {key!r}, which "
                    f"settings.xml never declares"
                )
                continue
            wanted = {"_string": "string", "_bool": "boolean", "_int": "integer"}[
                node.func.attr
            ]
            actual = SETTINGS_SCHEMA[key]["type"]
            if actual != wanted:
                problems.append(
                    f"{path}:{node.lineno}: reads setting {key!r} as {wanted}, "
                    f"but settings.xml declares it as {actual}; Kodi raises "
                    f"TypeError('Invalid setting type')"
                )
    return problems


@case("package: zip declares the addon root directory")
def package_root_directory(h):
    """Kodi's Android zip VFS needs a physical root directory entry.

    File members beneath the directory are insufficient: the Fire TV build
    reports that addon.xml cannot be opened even though the entry exists.
    """
    finished = subprocess.run(
        [sys.executable, os.path.join(REPO, "build.py")],
        capture_output=True,
        text=True,
    )
    if finished.returncode:
        return [f"build.py failed: {finished.stderr.strip()}"]
    version = ElementTree.parse(os.path.join(REPO, "addon.xml")).getroot().get("version")
    archive_path = os.path.join(REPO, "dist", f"plugin.video.immich-{version}.zip")
    with zipfile.ZipFile(archive_path) as archive:
        if "plugin.video.immich/" not in archive.namelist():
            return ["package lacks the plugin.video.immich/ directory entry"]
    return []


@case("static: each lib module imports standalone (no cycles, no path surprises)")
def static_module_imports(h):
    problems = []
    for module in ("api", "kodiutils", "listing", "router", "views"):
        code = (
            f"import sys; sys.path[:0] = [{STUBS!r}, {LIB!r}]; import {module}"
        )
        finished = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        if finished.returncode != 0:
            problems.append(
                f"importing {module!r} first fails:\n{finished.stderr.strip()}"
            )
    code = f"import sys; sys.path[:0] = [{STUBS!r}]; import runpy; " \
           f"sys.argv=['plugin://x/','-1','']; runpy.run_path({harness.ADDON_PY!r}, run_name='__main__')"
    finished = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if finished.returncode != 0:
        problems.append(
            f"addon.py cannot bootstrap in a clean interpreter:\n{finished.stderr.strip()}"
        )
    return problems


@case("static: every bundled art path handed to Kodi exists on disk")
def static_media_icons(h):
    """Checks the emitted art rather than instrumenting the media() helper.

    An `icon` key that is empty, or that points at a file which is not there,
    is precisely what makes Kodi fall back to DefaultVideo.png. So the values
    that reach addDirectoryItems are what matter, not which helper built them.
    """
    problems = []
    checked = 0
    routes = (
        "",
        "action=timeline",
        "action=albums",
        "action=people",
        "action=places",
        "action=tags",
        "action=memories",
        "action=bucket&id=2025-06-01",
    )
    for query in routes:
        h.reset()
        record = h.invoke(query)
        where = query or "(root)"
        for _url, item, _folder in record.items:
            if not item.getArt("icon"):
                problems.append(f"{where}: {item.label!r} has an empty 'icon' art key")
            for key in ("icon", "thumb", "poster", "fanart"):
                value = item.getArt(key)
                if not value or value.startswith(("http://", "https://")):
                    continue
                checked += 1
                if not os.path.exists(value):
                    problems.append(
                        f"{where}: {item.label!r} art[{key}] points at a missing "
                        f"file: {value}"
                    )
    if not checked:
        problems.append("no local art paths were emitted - instrumentation failed")
    return problems


# ==========================================================================
# Happy-path routes
# ==========================================================================


@case("route '': root menu", route="")
def route_root(h):
    h.reset()
    record = h.invoke("")
    problems = standard_checks(record, expect_content="files")
    labels = [item.label for _u, item, _f in record.items]
    for expected in ("Timeline", "Videos", "Albums", "Favourites", "People",
                     "Places", "Tags", "Memories", "Search", "Random", "Settings"):
        if expected not in labels:
            problems.append(f"root menu is missing {expected!r} (got {labels})")
    for url, item, isfolder in record.items:
        if item.label != "Settings" and not isfolder:
            problems.append(f"root entry {item.label!r} is not a folder")
        if item.isfolder != isfolder:
            problems.append(
                f"root entry {item.label!r}: setIsFolder({item.isfolder}) "
                f"disagrees with the addDirectoryItems flag {isfolder}"
            )
        if not item.getProperty("description"):
            problems.append(f"root entry {item.label!r} has no description")
    return problems


@case("route 'timeline': month list", route="timeline")
def route_timeline(h):
    h.reset()
    record = h.invoke("action=timeline")
    problems = standard_checks(record, expect_content="files")
    if len(record.items) != 3:
        problems.append(f"expected 3 month folders, got {len(record.items)}")
    for url, item, _f in record.items:
        query = dict(parse_qsl(urlparse(url).query))
        if query.get("action") != "bucket" or not query.get("id"):
            problems.append(f"month item URL is not a bucket link: {url!r}")
    # The server must have been asked with a pinned visibility.
    calls = [r for r in h.server.requests if r["path"] == "/api/timeline/buckets"]
    if not calls:
        problems.append("no /api/timeline/buckets request reached the server")
    elif calls[-1]["query"].get("visibility") != "timeline":
        problems.append(
            f"buckets requested without visibility=timeline: {calls[-1]['query']}"
        )
    return problems


@case("route 'bucket': one month of assets", route="bucket")
def route_bucket(h):
    h.reset()
    record = h.invoke("action=bucket&id=2026-08-01")
    problems = standard_checks(record, expect_content="images")
    if len(record.items) != 12:
        problems.append(f"expected 12 assets in the bucket, got {len(record.items)}")
    videos = [i for u, i, _ in record.items if "/video/playback" in u]
    stills = [i for u, i, _ in record.items if "/video/playback" not in u]
    if not videos:
        problems.append("columnar isImage=false never produced a video item")
    if not stills:
        problems.append("columnar isImage=true never produced a still")
    for item in stills:
        if item.video_tag_requested:
            problems.append(f"still {item.label!r} touched getVideoInfoTag")
        if not item.picture_tag_requested:
            problems.append(f"still {item.label!r} has no picture info tag")
    for item in videos:
        tag = item._video_tag
        if tag is None or tag.data.get("duration") != 83:
            got = None if tag is None else tag.data.get("duration")
            problems.append(
                f"video duration from the 2.7.5 'H:MM:SS.sss' string is {got!r}, "
                f"expected 83 seconds"
            )
    if record.categories and record.categories[0][1] != "August 2026":
        problems.append(
            f"bucket breadcrumb is {record.categories[0][1]!r}, expected "
            f"'August 2026'"
        )
    return problems


@case("route 'albums': album list", route="albums")
def route_albums(h):
    h.reset()
    record = h.invoke("action=albums")
    problems = standard_checks(record, expect_content="files")
    if len(record.items) != 2:
        problems.append(f"expected 2 albums, got {len(record.items)}")
    for url, item, _f in record.items:
        if item.label == "Untitled":
            # albumThumbnailAssetId is null: must still get the bundled icon.
            if not item.art.get("icon"):
                problems.append("album with a null thumbnail has no icon")
            if item.art.get("thumb", "").startswith("http") and "None" in item.art["thumb"]:
                problems.append(f"null thumbnail leaked into a URL: {item.art['thumb']}")
    return problems


@case("route 'album': one server-side page, in the album's own order", route="album")
def route_album(h):
    """Asserts the contract, not the endpoint.

    This used to require GET /api/albums/{id}, which embeds every asset with no
    paging: rendering page six of a 3000-asset album re-downloaded all 3000
    full DTOs. It now pages through search, so what matters is that the album
    is correctly filtered and ordered, not which call delivers it.
    """
    h.reset()
    record = h.invoke(f"action=album&id={ALBUM_1}&title=Holiday+2026&order=asc")
    problems = standard_checks(record, expect_content="images")
    if len(record.items) != 6:
        problems.append(f"expected 6 album assets, got {len(record.items)}")
    if record.categories and record.categories[0][1] != "Holiday 2026":
        problems.append(f"album category is {record.categories[0][1]!r}")

    posts = [r for r in h.server.requests if r["path"].endswith("/search/metadata")]
    if not posts:
        problems.append("the album was not fetched through a paged endpoint")
    else:
        body = posts[0]["body"]
        if body.get("albumIds") != [ALBUM_1]:
            problems.append(f"album filter missing from the search body: {body}")
        # An album's order is asc/desc, so it must be carried through or the
        # listing silently takes the server default instead of the album's.
        if body.get("order") != "asc":
            problems.append(f"the album's order was dropped: {body.get('order')!r}")
        if body.get("size") != h.settings_value("page_size"):
            problems.append(f"page size not honoured: {body.get('size')!r}")

    # The albums listing must forward the order, or the album route cannot know
    # it. `order` is optional on AlbumResponseDto, so an album that declares
    # none simply takes the server default at both ends.
    h.reset()
    listing = h.invoke("action=albums")
    declared = {a["id"]: a.get("order") for a in h.dataset.albums}
    checked = 0
    for url, item, _f in listing.items:
        query = dict(parse_qsl(urlparse(url).query))
        if query.get("action") != "album":
            continue
        want = declared.get(query.get("id"))
        if want:
            checked += 1
            if query.get("order") != want:
                problems.append(
                    f"album link for {item.label!r} dropped order={want!r}"
                )
    if not checked:
        problems.append("no album in the fixture declares an order to check")
    return problems


@case("edge: album with a null albumThumbnailAssetId and zero assets")
def route_album_empty(h):
    h.reset()
    record = h.invoke(f"action=album&id={ALBUM_2}&title=Untitled")
    problems = standard_checks(record, expect_content="images")
    if record.items:
        problems.append(f"empty album produced {len(record.items)} items")
    return problems


@case("route 'favourites': mutates request.params then re-enters timeline",
      route="favourites")
def route_favourites(h):
    h.reset()
    record = h.invoke("action=favourites")
    problems = standard_checks(record, expect_content="files")
    if not record.items:
        problems.append("favourites produced no month folders")
    calls = [r for r in h.server.requests if r["path"] == "/api/timeline/buckets"]
    if not calls:
        problems.append("favourites never queried the timeline")
    elif calls[-1]["query"].get("isFavorite") != "true":
        problems.append(
            f"favourites did not pass isFavorite=true: {calls[-1]['query']}"
        )
    for url, item, _f in record.items:
        query = dict(parse_qsl(urlparse(url).query))
        if query.get("favorite") != "1":
            problems.append(
                f"favourite month link loses the favorite filter: {url!r}"
            )
    if record.categories and record.categories[0][1] != "Favourites":
        problems.append(
            f"favourites category is {record.categories[0][1]!r}, expected "
            f"'Favourites'"
        )
    return problems


@case("route 'people': named, non-hidden people only", route="people")
def route_people(h):
    h.reset()
    record = h.invoke("action=people")
    problems = standard_checks(record, expect_content="files")
    labels = [i.label for _u, i, _f in record.items]
    if labels != ["Alice", "Bob"]:
        problems.append(f"expected ['Alice', 'Bob'], got {labels}")
    for url, item, _f in record.items:
        thumb = item.art.get("thumb", "")
        if "/api/people/" not in thumb:
            problems.append(f"person thumb is not a people thumbnail URL: {thumb!r}")
        check_kodi_url(thumb, problems, "person thumb")
    return problems


@case("route 'places': one folder per distinct city", route="places")
def route_places(h):
    h.reset()
    record = h.invoke("action=places")
    problems = standard_checks(record, expect_content="files")
    labels = [i.label for _u, i, _f in record.items]
    if labels != ["Amsterdam", "Lisbon"]:
        problems.append(f"expected ['Amsterdam', 'Lisbon'], got {labels}")
    return problems


@case("route 'place': assets in one city", route="place")
def route_place(h):
    h.reset()
    record = h.invoke("action=place&city=Amsterdam&title=Amsterdam")
    problems = standard_checks(record, expect_content="images")
    if not record.items:
        problems.append("place produced no assets")
    posts = [r for r in h.server.requests if r["path"] == "/api/search/metadata"]
    if not posts:
        problems.append("place did not POST /api/search/metadata")
    elif posts[0]["body"].get("city") != "Amsterdam":
        problems.append(f"search body missing city: {posts[0]['body']}")
    return problems


@case("route 'tags': tag list", route="tags")
def route_tags(h):
    h.reset()
    record = h.invoke("action=tags")
    problems = standard_checks(record, expect_content="files")
    labels = [i.label for _u, i, _f in record.items]
    if labels != ["Travel/Japan", "Family"]:
        problems.append(f"expected the nested tag values, got {labels}")
    for url, _i, _f in record.items:
        query = dict(parse_qsl(urlparse(url).query))
        if not query.get("tagId"):
            problems.append(f"tag link carries no tagId: {url!r}")
    return problems


@case("route 'memories': on-this-day folders", route="memories")
def route_memories(h):
    h.reset()
    record = h.invoke("action=memories")
    problems = standard_checks(record, expect_content="files")
    labels = [i.label for _u, i, _f in record.items]
    if labels != ["2019"]:
        problems.append(f"expected ['2019'] (the empty memory is skipped), got {labels}")
    return problems


@case("route 'memory': assets inside a memory", route="memory")
def route_memory(h):
    h.reset()
    record = h.invoke(f"action=memory&id={MEMORY_1}")
    problems = standard_checks(record, expect_content="images")
    if len(record.items) != 3:
        problems.append(f"expected 3 memory assets, got {len(record.items)}")
    return problems


@case("route 'memory': a memory that no longer exists fails gracefully")
def route_memory_missing(h):
    h.reset()
    record = h.invoke("action=memory&id=" + _uuid("m", 99))
    problems = standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    if record.exception is not None:
        problems.append(f"unhandled exception: {record.exception!r}")
    return problems


@case("route 'memory': no id at all")
def route_memory_no_id(h):
    h.reset()
    record = h.invoke("action=memory")
    problems = []
    if record.exception is not None:
        problems.append(f"unhandled exception: {record.exception!r}")
    if len(record.end_of_directory) != 1:
        problems.append(
            f"views.py:307 calls client.memory(None), which builds the path "
            f"/api/memories/None; endOfDirectory ran "
            f"{len(record.end_of_directory)} times"
        )
    return problems


@case("route 'search': search menu", route="search")
def route_search(h):
    h.reset()
    record = h.invoke("action=search")
    problems = standard_checks(record, expect_content="files")
    labels = [i.label for _u, i, _f in record.items]
    if labels != ["Search your photos", "Smart search"]:
        problems.append(f"unexpected search menu {labels}")
    h.reset()
    h.dataset.features["smartSearch"] = False
    record = h.invoke("action=search")
    problems += standard_checks(record, expect_content="files")
    labels = [i.label for _u, i, _f in record.items]
    if labels != ["Search your photos"]:
        problems.append(
            f"smartSearch=false should hide the smart entry, got {labels}"
        )
    return problems


@case("route 'search_text': keyboard input then metadata search", route="search_text")
def route_search_text(h):
    h.reset()
    harness.STATE.dialog_input_queue.append("IMG_030")
    record = h.invoke("action=search_text")
    problems = standard_checks(record, expect_content="images")
    if not record.items:
        problems.append("search returned nothing for a filename that exists")
    posts = [r for r in h.server.requests if r["path"] == "/api/search/metadata"]
    if not posts or posts[0]["body"].get("originalFileName") != "IMG_030":
        problems.append(f"first search body was {posts[0]['body'] if posts else None}")
    return problems


@case("route 'search_text': cancelled input aborts navigation")
def route_search_text_cancel(h):
    h.reset()
    record = h.invoke("action=search_text")
    return standard_checks(record, expect_succeeded=False, expect_content=None)


@case("route 'search_smart': CLIP search", route="search_smart")
def route_search_smart(h):
    h.reset()
    harness.STATE.dialog_input_queue.append("a dog on a beach")
    record = h.invoke("action=search_smart")
    problems = standard_checks(record, expect_content="images")
    posts = [r for r in h.server.requests if r["path"] == "/api/search/smart"]
    if not posts:
        problems.append("no /api/search/smart request")
    elif posts[0]["body"].get("query") != "a dog on a beach":
        problems.append(f"smart search body {posts[0]['body']}")
    return problems


@case("route 'random': unpaged random selection", route="random")
def route_random(h):
    h.reset()
    record = h.invoke("action=random")
    problems = standard_checks(record, expect_content="images")
    if len(record.items) != 12:
        problems.append(f"expected 12 random assets, got {len(record.items)}")
    posts = [r for r in h.server.requests if r["path"] == "/api/search/random"]
    if not posts:
        problems.append("no /api/search/random request")
    elif not isinstance(posts[0]["body"].get("size"), int):
        problems.append(f"random body has no integer size: {posts[0]['body']}")
    return problems


@case("route 'settings': opens the dialog and aborts navigation", route="settings")
def route_settings(h):
    h.reset()
    record = h.invoke("action=settings")
    problems = standard_checks(record, expect_succeeded=False, expect_content=None)
    if record.settings_opened != 1:
        problems.append(
            f"openSettings called {record.settings_opened} times, expected 1"
        )
    return problems


@case("route 'slideshow': RunPlugin builtin, no directory", route="slideshow")
def route_slideshow(h):
    h.reset()
    target = "plugin://plugin.video.immich/?action=timeline"
    from urllib.parse import quote
    record = h.invoke(f"action=slideshow&target={quote(target, safe='')}", handle=-1)
    problems = standard_checks(
        record, expect_directory=False, expect_content=None, allow_dialog=True
    )
    if len(record.builtins) != 1:
        problems.append(f"expected one executebuiltin, got {record.builtins}")
    else:
        builtin = record.builtins[0]
        if not builtin.startswith('SlideShow("'):
            problems.append(f"unexpected builtin {builtin!r}")
        if target not in builtin:
            problems.append(f"slideshow target was lost: {builtin!r}")
        # SlideShow(dir[,random|notrandom][,recursive][,pause][,beginslide=])
        # parses flags by name via CUtil::SplitParams, so assert presence
        # rather than position.
        for flag in ("notrandom", "recursive"):
            if flag not in builtin:
                problems.append(f"slideshow is missing the {flag!r} flag: {builtin!r}")
        if "random," in builtin and "notrandom" not in builtin:
            problems.append(f"slideshow asked for random order: {builtin!r}")
    return problems


@case("route 'test_connection': reports the signed-in user", route="test_connection")
def route_test_connection(h):
    h.reset()
    record = h.invoke("action=test_connection", handle=-1)
    problems = standard_checks(
        record, expect_directory=False, expect_content=None, allow_dialog=True
    )
    oks = [d for d in record.dialogs if d[0] == "ok"]
    if len(oks) != 1:
        problems.append(f"expected one ok dialog, got {record.dialogs}")
    else:
        heading, message = oks[0][1], oks[0][2]
        if heading != "Connected":
            problems.append(f"heading {heading!r}")
        if "Admin User" not in message or "2.7.5" not in message:
            problems.append(f"message does not name the user and version: {message!r}")
    return problems


@case("dispatch: an unknown action falls back to the root menu")
def route_unknown_action(h):
    h.reset()
    record = h.invoke("action=not_a_route")
    problems = standard_checks(record, expect_content="files")
    if not record.items:
        problems.append("unknown action produced an empty listing")
    return problems


@case("deprecated: ?action=timeline&video=1 is adapted onto the videos route")
def route_videos(h):
    """The Videos entry in 1.0.0 and 2.0.0, so it lives in saved favourites.

    It must keep working, and it must not keep its old behaviour: it used to
    list every month with a full asset count, and photo-only months opened
    empty. Adapted rather than merely tolerated, so an old favourite now gets
    the same listing as the current menu entry.
    """
    h.reset()
    legacy = h.invoke("action=timeline&video=1")
    problems = standard_checks(legacy, expect_content="videos")

    h.reset()
    current = h.invoke("action=videos")

    legacy_urls = [url for url, _i, _f in legacy.items]
    current_urls = [url for url, _i, _f in current.items]
    if legacy_urls != current_urls:
        problems.append(
            f"the legacy URL and action=videos disagree: "
            f"{len(legacy_urls)} vs {len(current_urls)} items"
        )
    if not legacy_urls:
        problems.append("the legacy URL now lists nothing")
    for url in legacy_urls:
        if "/video/playback" not in url:
            problems.append(f"a still leaked into the legacy listing: {url!r}")
    for _url, _item, isfolder in legacy.items:
        if isfolder:
            problems.append("the legacy URL still emits month folders")

    # The plain timeline must be untouched by the adapter.
    h.reset()
    plain = h.invoke("action=timeline")
    problems += standard_checks(plain, expect_content="files")
    if not any(f for _u, _i, f in plain.items):
        problems.append("the plain timeline stopped emitting month folders")
    return problems


@case("deprecated: no dead code left behind by the 2.0.2 version floor")
def dead_code(h):
    """Removals that the Kodi 20 floor and the videos route made unreachable."""
    problems = []
    # Parsed, not grepped: the source explains why the old getters are gone,
    # and naming them in a comment is not calling them.
    path = os.path.join(LIB, "kodiutils.py")
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    retired = {"getSettingString", "getSettingBool", "getSettingInt"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in retired:
            problems.append(
                f"kodiutils.py:{node.lineno} calls {node.attr}; xbmc.python "
                f"3.0.1 guarantees Settings, so the fallback is unreachable"
            )
        if isinstance(node, ast.Attribute) and node.attr == "getSettings":
            continue
    names = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    if "ADDON_NAME" in names:
        problems.append("kodiutils.py still defines ADDON_NAME, which nothing reads")
    listing = open(os.path.join(LIB, "listing.py"), encoding="utf-8").read()
    if "_duration_label" in listing:
        problems.append("listing.py still defines _duration_label, which nothing calls")
    if os.path.exists(os.path.join(MEDIA_DIR, "slideshow.png")):
        problems.append("resources/media/slideshow.png ships but nothing references it")
    return problems


# ==========================================================================
# Failure paths
# ==========================================================================


@case("failure: server unreachable (connection refused)")
def fail_connection_refused(h):
    h.reset()
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
    probe.close()
    h.set_setting("immich_url", f"http://127.0.0.1:{dead}")
    record = h.invoke("action=timeline")
    problems = standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    oks = [d for d in record.dialogs if d[0] == "ok"]
    if not oks or oks[0][1] != "Connection error":
        problems.append(f"expected the connection-error dialog, got {record.dialogs}")
    return problems


@case("failure: HTTP 401 from every authenticated endpoint")
def fail_401(h):
    h.reset()
    h.dataset.force_status["/timeline"] = (401, "Invalid API key")
    record = h.invoke("action=timeline")
    problems = standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    oks = [d for d in record.dialogs if d[0] == "ok"]
    if not oks or oks[0][1] != "Authorization error":
        problems.append(f"expected the auth dialog, got {record.dialogs}")
    return problems


@case("failure: HTTP 500")
def fail_500(h):
    h.reset()
    h.dataset.force_status["/timeline"] = (500, "Internal server error")
    record = h.invoke("action=timeline")
    problems = standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    oks = [d for d in record.dialogs if d[0] == "ok"]
    if not oks:
        problems.append(f"a 500 produced no dialog: {record.dialogs}")
        return problems
    heading, message = oks[0][1], oks[0][2]
    # The heading must come from strings.po, not be hardcoded English.
    if heading not in set(STRINGS.values()):
        problems.append(
            f"a 500 used the hardcoded heading {heading!r}; it is not in strings.po"
        )
    # The user should see Immich's message, not its JSON error envelope.
    if "{" in message or "statusCode" in message:
        problems.append(f"a 500 leaked the raw JSON body to the user: {message!r}")
    if "Internal server error" not in message:
        problems.append(f"a 500 dropped the server's message: {message!r}")
    return problems


@case("failure: malformed JSON body with a 200 status")
def fail_malformed_json(h):
    h.reset()
    h.dataset.malformed.add("/timeline")
    record = h.invoke("action=timeline")
    return standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )


@case("edge: an entirely empty library")
def edge_empty_library(h):
    h.reset()
    h.dataset.buckets = []
    h.dataset.bucket_sizes = {}
    h.dataset.albums = []
    h.dataset.people = {"people": [], "total": 0, "hidden": 0, "hasNextPage": False}
    h.dataset.tags = []
    h.dataset.memories = []
    h.dataset.cities = []
    h.dataset.search_results = []
    h.dataset.random_results = []
    problems = []
    for query, content in (
        ("action=timeline", "files"),
        ("action=albums", "files"),
        ("action=people", "files"),
        ("action=places", "files"),
        ("action=tags", "files"),
        ("action=memories", "files"),
        ("action=random", "images"),
    ):
        record = h.invoke(query)
        problems += standard_checks(record, expect_content=content)
        if record.items:
            problems.append(f"{query}: empty library still produced items")
    return problems


@case("edge: columnar bucket with mismatched array lengths")
def edge_columnar_mismatch(h):
    h.reset()
    h.dataset.bucket_mismatch = True
    record = h.invoke("action=bucket&id=2026-08-01")
    problems = standard_checks(record, expect_content="images")
    if len(record.items) != 12:
        problems.append(
            f"a truncated column changed the item count to {len(record.items)}"
        )
    videos = [u for u, _i, _f in record.items if "/video/playback" in u]
    if videos:
        problems.append(
            "api.py:441-444 replaces a short column wholesale with defaults, so "
            f"the truncated isImage column silently reclassified assets ({len(videos)} videos)"
        )
    return problems


@case("edge: asset with null city, country and duration")
def edge_null_fields(h):
    h.reset()
    h.dataset.bucket_nulls = True
    record = h.invoke("action=bucket&id=2026-07-01")
    problems = standard_checks(record, expect_content="images")
    for _u, item, _f in record.items:
        if item.label2 not in ("", None):
            problems.append(f"null city/country produced label2 {item.label2!r}")
        if "None" in (item.getProperty("plot") or ""):
            problems.append("a None leaked into the plot text")
    return problems


@case("edge: blank immich_url goes straight to the settings dialog")
def edge_blank_url(h):
    h.reset()
    h.set_setting("immich_url", "")
    record = h.invoke("action=timeline")
    problems = standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    oks = [d for d in record.dialogs if d[0] == "ok"]
    if not oks or oks[0][1] != "Not configured":
        problems.append(f"expected the 'Not configured' dialog, got {record.dialogs}")
    if record.settings_opened != 1:
        problems.append("the settings dialog was not opened")
    if h.server.requests:
        problems.append("a request was made despite there being no server URL")
    return problems


@case("edge: immich_url behind a reverse-proxy subpath")
def edge_subpath_url(h):
    h.reset()
    h.dataset.path_prefix = "/immich"
    h.set_setting("immich_url", h.server.url + "/immich")
    record = h.invoke("action=timeline")
    problems = []
    paths = [r["path"] for r in h.server.requests]
    if any(not p.startswith("/immich/") for p in paths):
        problems.append(
            "api.py:292 builds the request path as '/api' + path from the parsed "
            "netloc only, dropping any base-URL path prefix. Requests went to "
            f"{sorted(set(paths))} instead of /immich/api/... . With "
            f"immich_url={h.server.url + '/immich'!r} every API call 404s while "
            "the thumbnail/playback URLs handed to Kodi (api.py:373, which uses "
            "the full base_url) still carry the prefix, so the two disagree."
        )
    if record.exception is not None:
        problems.append(f"unhandled exception: {record.exception!r}")
    return problems


# ==========================================================================
# Paging
# ==========================================================================


@case("paging: a large bucket splits into pages that round-trip")
def paging(h):
    h.reset(page_size=50)
    h.dataset.buckets = [{"timeBucket": "2026-08-01", "count": 130}]
    h.dataset.bucket_sizes = {"2026-08-01": 130}

    problems = []
    query = "action=bucket&id=2026-08-01"
    seen_pages = []
    for expected_items, expect_next in ((50, True), (50, True), (30, False)):
        record = h.invoke(query)
        problems += standard_checks(record, expect_content="images")
        next_items = [
            (u, i) for u, i, folder in record.items if folder and i.label == "Next page"
        ]
        assets = len(record.items) - len(next_items)
        if assets != expected_items:
            problems.append(
                f"{query}: expected {expected_items} assets on this page, got {assets}"
            )
        if expect_next and not next_items:
            problems.append(f"{query}: no 'Next page' item but {130 - assets} remain")
            break
        if not expect_next:
            if next_items:
                problems.append(f"{query}: the final page still offers a next page")
            break
        next_url, next_item = next_items[0]
        parsed = dict(parse_qsl(urlparse(next_url).query))
        if parsed.get("action") != "bucket" or parsed.get("id") != "2026-08-01":
            problems.append(f"next-page URL loses its context: {next_url!r}")
        page = parsed.get("page")
        seen_pages.append(page)
        if not next_item.art.get("icon"):
            problems.append("the 'Next page' item has no icon")
        query = urlparse(next_url).query
    if seen_pages != ["1", "2"]:
        problems.append(f"page numbers did not advance 1 then 2: {seen_pages}")
    return problems


@case("paging: a dialog-driven search loses its query on page 2")
def paging_search_query(h):
    h.reset(page_size=50)
    h.dataset.search_results = [
        __import__("mockimmich").asset_dto(i) for i in range(300, 420)
    ]
    harness.STATE.dialog_input_queue.append("IMG_03")
    record = h.invoke("action=search_text")
    problems = standard_checks(record, expect_content="images")
    next_items = [
        u for u, i, folder in record.items if folder and i.label == "Next page"
    ]
    if not next_items:
        problems.append("120 results with page_size=50 produced no 'Next page' item")
        return problems
    parsed = dict(parse_qsl(urlparse(next_items[0]).query))
    if "q" not in parsed:
        problems.append(
            "views.py:436 builds the next-page URL from request.params, which for "
            "a keyboard-driven search contains only action=search_text. Following "
            f"{next_items[0]!r} re-opens the keyboard instead of showing page 2 of "
            "the same search."
        )
    return problems


# ==========================================================================
# reuselanguageinvoker
# ==========================================================================


@case("reuse: several routes in one interpreter do not contaminate each other")
def reuse_sequence(h):
    h.reset()
    sequence = [
        ("action=timeline", "files", 1),
        ("action=bucket&id=2026-08-01", "images", 2),
        ("action=albums", "files", 3),
        ("action=people", "files", 4),
        ("", "files", 5),
        ("action=tags", "files", 6),
        ("action=bucket&id=2026-07-01", "images", 7),
    ]
    problems = []
    for query, content, handle in sequence:
        record = h.invoke(query, handle=handle)
        problems += standard_checks(record, expect_content=content)
        for eod_handle, _s, _u, _c in record.end_of_directory:
            if eod_handle != handle:
                problems.append(
                    f"{query}: endOfDirectory used a stale handle {eod_handle} "
                    f"instead of {handle}"
                )
        for dir_handle, _entries, _total in record.directory_items:
            if dir_handle != handle:
                problems.append(
                    f"{query}: addDirectoryItems used a stale handle {dir_handle}"
                )
    counts = [len(r.items) for r in h.invocations[-len(sequence):]]
    if counts[1] != 12 or counts[6] != 3:
        problems.append(f"bucket contents leaked between invocations: {counts}")
    return problems


@case("reuse: the cached server version is not invalidated when the URL changes")
def reuse_session_cache(h):
    h.reset()
    h.invoke("action=timeline")
    from mockimmich import MockImmich
    other = MockImmich().start()
    try:
        other.dataset.version = {"major": 1, "minor": 132, "patch": 0}
        other.dataset.albums = h.dataset.albums
        other.dataset.album_assets = h.dataset.album_assets
        h.set_setting("immich_url", other.url)
        record = h.invoke("action=albums")
        problems = standard_checks(record, expect_content="files")
        asked = [r for r in other.requests if r["path"] == "/api/server/version"]
        if not asked:
            problems.append(
                "api.py:315-336 caches the detected version on the home window "
                "(kodiutils.py:54) and nothing clears it when immich_url changes. "
                "After switching to a server reporting 1.132.0 the addon kept the "
                "2.7.5 branch and never re-probed /api/server/version, so it will "
                "parse a bare-array timeline bucket as columnar."
            )
        return problems
    finally:
        other.stop()


@case("hygiene: the API key travels as a header on every request")
def api_key_header(h):
    h.reset()
    for query in ("action=timeline", "action=albums", "action=people",
                  "action=tags", "action=memories", "action=places"):
        h.invoke(query)
    problems = []
    for request in h.server.requests:
        if request["path"] in ("/api/server/version", "/api/server/features"):
            continue
        if request["api_key"] != API_KEY:
            problems.append(
                f"{request['method']} {request['path']} sent x-api-key="
                f"{request['api_key']!r}"
            )
        if "key=" in (request["query"].get("key") or ""):
            problems.append(f"{request['path']} put the key in the query string")
    return problems


@case("hygiene: no route asks the timeline for a param Immich 2.7.5 dropped")
def dead_params(h):
    h.reset()
    for query in ("action=timeline", "action=bucket&id=2026-08-01",
                  "action=favourites"):
        h.invoke(query)
    problems = []
    dead = ("size", "isArchived", "page", "pageSize")
    for request in h.server.requests:
        if not request["path"].startswith("/api/timeline"):
            continue
        for name in dead:
            if name in request["query"]:
                problems.append(
                    f"{request['path']} still sends the removed param "
                    f"{name}={request['query'][name]!r}"
                )
    return problems


@case("labels: month headings are clean, asset labels use the Kodi region format")
def label_formats(h):
    h.reset()
    record = h.invoke("action=timeline")
    problems = []
    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday")
    for _u, item, _f in record.items:
        if any(day in item.label for day in weekdays):
            problems.append(f"month heading carries a weekday: {item.label!r}")
        if "  " in item.label or item.label.strip(" ,-/") != item.label:
            problems.append(f"month heading has stray separators: {item.label!r}")
        if "%" in item.label:
            problems.append(f"month heading has a raw strftime token: {item.label!r}")

    # Kodi's en_GB long date is "%A, %-d %B %Y"; the addon must apply it.
    record = h.invoke("action=bucket&id=2026-08-01")
    for _u, item, _f in record.items:
        if "%" in item.label:
            problems.append(f"asset label has a raw strftime token: {item.label!r}")
        break
    return problems


@case("labels: an Android-style region format without %-d still renders")
def label_region_variants(h):
    saved = dict(xbmc.REGION_FORMATS)
    problems = []
    try:
        for datelong, timefmt in (
            ("DDDD, MMMM D, YYYY", "hh:mm:ss xx"),   # en_US
            ("D. MMMM YYYY", "HH:mm:ss"),            # de_DE
            ("YYYY'年'M'月'D'日'", "HH:mm:ss"),        # ja_JP, quoted literals
        ):
            xbmc.REGION_FORMATS["datelong"] = datelong
            xbmc.REGION_FORMATS["time"] = timefmt
            code = (
                "import sys; sys.path[:0] = [%r, %r];\n"
                "import xbmc; xbmc.REGION_FORMATS['datelong'] = %r;\n"
                "xbmc.REGION_FORMATS['time'] = %r;\n"
                "import listing, datetime;\n"
                "print(listing.format_datetime(datetime.datetime(2026, 8, 1, 14, 5, 6)))"
                % (STUBS, LIB, datelong, timefmt)
            )
            finished = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True
            )
            if finished.returncode != 0:
                problems.append(
                    f"listing.format_datetime crashes with datelong={datelong!r}: "
                    f"{finished.stderr.strip().splitlines()[-1]}"
                )
            elif "%" in finished.stdout:
                problems.append(
                    f"datelong={datelong!r} left a raw token in "
                    f"{finished.stdout.strip()!r}"
                )
    finally:
        xbmc.REGION_FORMATS.clear()
        xbmc.REGION_FORMATS.update(saved)
    return problems


@case("labels: a libc that emits -d for %-d still renders the day")
def label_broken_no_pad_day(h):
    """LibreELEC's strftime may copy the unsupported no-pad token as `-d`.

    It does not raise ValueError, so this models the exact broken label rather
    than the Android failure mode covered above.
    """
    class BrokenNoPadDay:
        def strftime(self, fmt):
            if "%d" in fmt:
                return "05"
            return fmt.replace("%-d", "-d")

    import listing

    result = listing._strftime(BrokenNoPadDay(), "Sunday, %-d. July 2026 19:21:00")
    problems = []
    if result != "Sunday, 5. July 2026 19:21:00":
        problems.append(f"unsupported %-d rendered as {result!r}")
    return problems


# ==========================================================================
# Immich version matrix
#
# Shapes and parameter windows come from immich-api-reference.md. The addon
# claims "from 1.13x through 3.x" (addon.xml news), so each branch it takes on
# version gets driven against a server that really behaves that way.
# ==========================================================================


def _timeline_queries(h):
    return [r["query"] for r in h.server.requests
            if r["path"].endswith(("/timeline/buckets", "/timeline/bucket"))]


def _matrix_hygiene(h, version):
    """Params this server version would silently drop, plus required ones."""
    problems = list(h.server.param_violations)
    if parse_version(version) < (1, 133, 0):
        for query in _timeline_queries(h):
            if "size" not in query:
                problems.append(
                    "api.py:540 _timeline_params() never emits a size param. "
                    "It is optional only from v1.133.0 (reference section 1); on "
                    f"Immich {version} both /timeline/buckets and "
                    "/timeline/bucket require size=DAY|MONTH and 400 without it. "
                    f"Sent: {query}"
                )
                break
    return problems


@case("immich 1.132.0: size=MONTH is sent, so the timeline works")
def matrix_1132_size(h):
    """`size` was mandatory on both timeline endpoints until v1.133 removed it.

    Omitting it makes every listing 400 on an older server, so the client must
    send it below that boundary and must not send it above.
    """
    h.reset()
    h.set_version("1.132.0")
    record = h.invoke("action=timeline")
    problems = standard_checks(record, expect_content="files")
    if not record.items:
        problems.append("the 1.132 timeline produced no months")

    buckets = [r for r in h.server.requests if r["path"] == "/api/timeline/buckets"]
    if not buckets:
        problems.append("/api/timeline/buckets was never called")
    for call in buckets:
        if call["query"].get("size") != "MONTH":
            problems.append(
                f"1.132 requires size=MONTH on /timeline/buckets, sent {call['query']}"
            )

    # And the same param must be absent once the server no longer accepts it.
    h.reset()
    h.set_version("2.7.5")
    h.invoke("action=timeline")
    for call in [r for r in h.server.requests if r["path"] == "/api/timeline/buckets"]:
        if "size" in call["query"]:
            problems.append(f"2.7.5 must not send size, sent {call['query']}")

    problems += _matrix_hygiene(h, "2.7.5")
    return problems


@case("immich 1.132.0: bare-array bucket parses, and visibility is not sent")
def matrix_1132_shape(h):
    h.reset()
    h.set_version("1.132.0")
    # Isolate the response shape from the missing-size defect above.
    h.dataset.enforce_legacy_size = False

    problems = standard_checks(h.invoke("action=timeline"), expect_content="files")
    record = h.invoke("action=bucket&id=2026-08-01")
    problems += standard_checks(record, expect_content="images")

    if len(record.items) != 12:
        problems.append(
            f"the pre-1.133 bare AssetResponseDto array yielded "
            f"{len(record.items)} items, expected 12"
        )
    for _u, item, _f in record.items:
        if not item.label:
            problems.append("bare-array asset produced a blank label")
            break
    videos = [i for u, i, _f in record.items if "/video/playback" in u]
    if not videos:
        problems.append("no video was recognised from type=VIDEO in the array")
    for query in _timeline_queries(h):
        if "visibility" in query:
            problems.append(
                f"visibility is a v1.133+ param but was sent to 1.132.0: {query}"
            )
        if "isArchived" in query:
            problems.append(f"isArchived sent without an archive filter: {query}")
    problems += list(h.server.param_violations)
    return problems


@case("immich 1.132.0: image_quality=fullsize degrades to preview")
def matrix_1132_fullsize(h):
    h.reset(image_quality=1)
    h.set_version("1.132.0")
    h.dataset.enforce_legacy_size = False
    record = h.invoke("action=bucket&id=2026-08-01")
    problems = standard_checks(record, expect_content="images")
    offenders = [u for u, _i, _f in record.items if "size=fullsize" in u]
    if offenders:
        problems.append(
            "image_url() emitted ?size=fullsize on 1.132.0. That value was "
            "only added in v1.133.0 (reference section 5); before it the size "
            "enum accepts thumbnail|preview only, so the request 400s and the "
            "still never renders. It must fall back to preview below that "
            f"boundary. Example URL: {offenders[0].split('|')[0]}"
        )
    return problems


@case("immich 1.134.0: columnar localDateTime still resolves a taken-at")
def matrix_1134(h):
    h.reset()
    h.set_version("1.134.0")
    record = h.invoke("action=bucket&id=2026-08-01")
    problems = standard_checks(record, expect_content="images")
    if len(record.items) != 12:
        problems.append(f"expected 12 items, got {len(record.items)}")
    for _u, item, _f in record.items:
        if not item.label:
            problems.append("localDateTime-only columnar produced a blank label")
            break
        if "11:12:33" not in item.label:
            problems.append(
                f"localDateTime 11:12:33 did not reach the label: {item.label!r}"
            )
            break
        if not item.datetime:
            problems.append(f"item {item.label!r} has no setDateTime value")
            break
    problems += list(h.server.param_violations)
    return problems


@case("immich 1.140.0: fileCreatedAt plus a fractional localOffsetHours")
def matrix_1140(h):
    h.reset()
    h.set_version("1.140.0")
    h.dataset.offset_hours = 5.5
    record = h.invoke("action=bucket&id=2026-08-01")
    problems = standard_checks(record, expect_content="images")
    if len(record.items) != 12:
        problems.append(f"expected 12 items, got {len(record.items)}")
    # 09:12:33 UTC + 5.5h == 14:42:33 local wall clock.
    for _u, item, _f in record.items:
        if "14:42:33" not in item.label:
            problems.append(
                "api.py:496-501 did not apply localOffsetHours=5.5 to "
                f"fileCreatedAt 09:12:33; label is {item.label!r}, expected the "
                "local wall clock 14:42:33"
            )
        if item.datetime and not item.datetime.endswith("T14:42:33"):
            problems.append(f"setDateTime is {item.datetime!r}, expected T14:42:33")
        break
    problems += list(h.server.param_violations)
    return problems


@case("immich 3.1.0: album assets come from search, durations are integer ms")
def matrix_310_album(h):
    h.reset()
    h.set_version("3.1.0")
    record = h.invoke(f"action=album&id={ALBUM_1}&title=Holiday+2026")
    problems = standard_checks(record, expect_content="images")

    if len(record.items) != 6:
        problems.append(
            f"v3 album fallback listed {len(record.items)} assets, expected 6"
        )
    if any(r["path"].endswith(f"/albums/{ALBUM_1}") for r in h.server.requests):
        problems.append(
            "the addon still called GET /api/albums/{id} on 3.1.0, where the "
            "response has no assets key at all"
        )
    posts = [r for r in h.server.requests if r["path"].endswith("/search/metadata")]
    if not posts:
        problems.append("v3 album did not fall back to POST /api/search/metadata")
    elif posts[0]["body"].get("albumIds") != [ALBUM_1]:
        problems.append(f"search body lacks albumIds: {posts[0]['body']}")

    videos = [i for u, i, _f in record.items if "/video/playback" in u]
    if not videos:
        problems.append("no video item in the v3 album listing")
    for item in videos:
        got = item._video_tag.data.get("duration") if item._video_tag else None
        if got != 83:
            problems.append(
                f"v3 integer-millisecond duration 83456 rendered as {got!r} "
                f"seconds, expected 83"
            )
    stills = [i for u, i, _f in record.items if "/video/playback" not in u]
    for item in stills:
        if item.video_tag_requested:
            problems.append("a v3 still with duration=null got a video info tag")
    problems += list(h.server.param_violations)
    return problems


@case("immich 3.1.0: shared_only sends isShared, not the removed shared param")
def matrix_310_shared(h):
    h.reset(shared_only=True)
    h.set_version("3.1.0")
    record = h.invoke("action=albums")
    problems = standard_checks(record, expect_content="files")
    calls = [r for r in h.server.requests if r["path"].endswith("/api/albums")]
    if not calls:
        problems.append("no GET /api/albums request")
    else:
        query = calls[-1]["query"]
        if query.get("isShared") != "true":
            problems.append(f"expected isShared=true on 3.1.0, got {query}")
        if "shared" in query:
            problems.append(f"the v3-removed shared param was still sent: {query}")
    problems += list(h.server.param_violations)
    return problems


@case("immich 3.1.0: the rest of the routes survive the v3 shapes")
def matrix_310_routes(h):
    h.reset()
    h.set_version("3.1.0")
    problems = []
    for query, content in (
        ("", "files"),
        ("action=timeline", "files"),
        ("action=bucket&id=2026-08-01", "images"),
        ("action=albums", "files"),
        ("action=people", "files"),
        ("action=places", "files"),
        ("action=tags", "files"),
        ("action=memories", "files"),
        ("action=random", "images"),
    ):
        problems += standard_checks(h.invoke(query), expect_content=content)
    problems += list(h.server.param_violations)
    return problems


@case("immich 2.7.5: no request carries a param this version dropped or lacks")
def matrix_275_params(h):
    h.reset(shared_only=True, include_partners=True)
    for query in ("", "action=timeline", "action=bucket&id=2026-08-01",
                  "action=albums", f"action=album&id={ALBUM_1}",
                  "action=favourites", "action=people", "action=places",
                  "action=place&city=Amsterdam", "action=tags",
                  "action=memories", f"action=memory&id={MEMORY_1}",
                  "action=random"):
        h.invoke(query)
    return list(h.server.param_violations)


@case("immich 1.134.0: an empty album lists nothing, not the whole library")
def matrix_1134_album_ids(h):
    h.reset()
    h.set_version("1.134.0")
    record = h.invoke(f"action=album&id={ALBUM_2}&title=Untitled")
    problems = standard_checks(record, expect_content="images")
    if record.items:
        problems.append(
            "an album with assetCount 0 listed "
            f"{len(record.items)} assets. album_assets() must treat an empty "
            "embedded list as empty rather than falling through to "
            "search_metadata(albumIds=[id]): albumIds only exists from v1.135.0 "
            "(reference section 6) and the server strips unknown body keys "
            "instead of rejecting them (reference section 0), so the filter "
            "would vanish and the whole library would be listed."
        )
    return problems


# ==========================================================================
# Settings variations
# ==========================================================================


def _still_urls(record):
    return [u for u, _i, _f in record.items
            if "/api/assets/" in u and "/video/playback" not in u]


@case("setting image_quality: 0 preview, 1 fullsize, 2 original")
def setting_image_quality(h):
    problems = []
    for value, expected in (
        (0, "/thumbnail?size=preview"),
        (1, "/thumbnail?size=fullsize"),
        (2, "/original"),
    ):
        h.reset(image_quality=value)
        record = h.invoke("action=bucket&id=2026-08-01")
        problems += standard_checks(record, expect_content="images")
        urls = _still_urls(record)
        if not urls:
            problems.append(f"image_quality={value}: no still URLs emitted")
            continue
        head = urls[0].split("|")[0]
        if not head.endswith(expected):
            problems.append(
                f"image_quality={value}: still URL is {head!r}, expected it to "
                f"end with {expected!r}"
            )
        # The grid thumbnail must stay small whatever the open-quality is.
        thumb = record.items[0][1].art.get("thumb", "").split("|")[0]
        if not thumb.endswith("/thumbnail?size=thumbnail"):
            problems.append(
                f"image_quality={value}: grid thumb is {thumb!r}, expected "
                f"?size=thumbnail"
            )
    return problems


@case("setting asset_name: date vs original filename, with a bucket fallback")
def setting_asset_name(h):
    problems = []

    # A timeline bucket has no filenames at all (reference section 3).
    for mode in (0, 1):
        h.reset(asset_name=mode)
        record = h.invoke("action=bucket&id=2026-08-01")
        problems += standard_checks(record, expect_content="images")
        for _u, item, _f in record.items:
            if not item.label:
                problems.append(f"asset_name={mode}: blank label in a bucket")
            elif not any(ch.isdigit() for ch in item.label):
                problems.append(
                    f"asset_name={mode}: bucket label {item.label!r} is neither "
                    f"a date nor a filename"
                )
            break

    # An album carries full DTOs, so mode 1 must actually use the filename.
    h.reset(asset_name=0)
    dated = h.invoke(f"action=album&id={ALBUM_1}&title=x")
    problems += standard_checks(dated, expect_content="images")
    h.reset(asset_name=1)
    named = h.invoke(f"action=album&id={ALBUM_1}&title=x")
    problems += standard_checks(named, expect_content="images")

    date_labels = [i.label for _u, i, _f in dated.items]
    name_labels = [i.label for _u, i, _f in named.items]
    if not name_labels or not all(l.startswith("IMG_") for l in name_labels):
        problems.append(f"asset_name=1 did not use originalFileName: {name_labels}")
    if date_labels == name_labels:
        problems.append("asset_name made no difference to album labels")
    if any(l.startswith("IMG_") for l in date_labels):
        problems.append(f"asset_name=0 used a filename anyway: {date_labels}")
    return problems


@case("setting shared_only: GET /api/albums carries the version's shared flag")
def setting_shared_only(h):
    problems = []
    for version, flag in (("2.7.5", "shared"), ("3.1.0", "isShared")):
        h.reset(shared_only=True)
        h.set_version(version)
        record = h.invoke("action=albums")
        problems += standard_checks(record, expect_content="files")
        calls = [r for r in h.server.requests if r["path"].endswith("/api/albums")]
        if not calls:
            problems.append(f"{version}: no GET /api/albums")
            continue
        if calls[-1]["query"].get(flag) != "true":
            problems.append(
                f"{version}: expected {flag}=true, got {calls[-1]['query']}"
            )
        labels = [i.label for _u, i, _f in record.items]
        if labels != ["Untitled"]:
            problems.append(f"{version}: shared filter listed {labels}")

    # And with the setting off, no flag at all.
    h.reset(shared_only=False)
    h.invoke("action=albums")
    calls = [r for r in h.server.requests if r["path"].endswith("/api/albums")]
    if calls and ("shared" in calls[-1]["query"] or "isShared" in calls[-1]["query"]):
        problems.append(f"shared_only=false still sent a flag: {calls[-1]['query']}")
    return problems


@case("setting include_partners: only on a plain timeline")
def setting_include_partners(h):
    problems = []

    h.reset(include_partners=True)
    h.invoke("action=timeline")
    plain = _timeline_queries(h)
    if not plain or plain[-1].get("withPartners") != "true":
        problems.append(f"withPartners missing from a plain timeline: {plain}")
    if plain and plain[-1].get("visibility") != "timeline":
        problems.append(
            f"withPartners requires visibility=timeline or Immich 400s: {plain}"
        )

    # Immich 400s on withPartners together with isFavorite.
    h.reset(include_partners=True)
    h.invoke("action=favourites")
    fav = _timeline_queries(h)
    for query in fav:
        if query.get("withPartners"):
            problems.append(
                f"withPartners sent alongside isFavorite, which Immich rejects "
                f"with a 400: {query}"
            )
    if not any(q.get("isFavorite") == "true" for q in fav):
        problems.append(f"favourites lost its isFavorite filter: {fav}")

    # withPartners is a no-op once albumId narrows the query.
    h.reset(include_partners=True)
    h.invoke(f"action=timeline&albumId={ALBUM_1}")
    scoped = _timeline_queries(h)
    for query in scoped:
        if query.get("withPartners"):
            problems.append(f"withPartners sent on an album-scoped timeline: {query}")
        if query.get("albumId") != ALBUM_1:
            problems.append(f"albumId did not reach the timeline: {query}")

    h.reset(include_partners=False)
    h.invoke("action=timeline")
    off = _timeline_queries(h)
    if off and off[-1].get("withPartners"):
        problems.append(f"include_partners=false still sent withPartners: {off}")
    return problems


@case("setting show_videos_in_timeline: false hides videos only in the timeline")
def setting_show_videos(h):
    problems = []

    h.reset(show_videos_in_timeline=False)
    bucket = h.invoke("action=bucket&id=2026-08-01")
    problems += standard_checks(bucket, expect_content="images")
    if any("/video/playback" in u for u, _i, _f in bucket.items):
        problems.append("show_videos_in_timeline=false left videos in the bucket")
    if not bucket.items:
        problems.append("hiding videos emptied the bucket entirely")

    album = h.invoke(f"action=album&id={ALBUM_1}&title=x")
    problems += standard_checks(album, expect_content="images")
    if not any("/video/playback" in u for u, _i, _f in album.items):
        problems.append(
            "views.py _emit_assets applied the timeline video preference to an "
            "album listing, which the user asked for by name"
        )

    harness.STATE.dialog_input_queue.append("IMG_030")
    search = h.invoke("action=search_text")
    problems += standard_checks(search, expect_content="images")
    if not any("/video/playback" in u for u, _i, _f in search.items):
        problems.append(
            "the timeline video preference also hid videos from search results"
        )

    h.reset(show_videos_in_timeline=True)
    both = h.invoke("action=bucket&id=2026-08-01")
    problems += standard_checks(both, expect_content="images")
    if not any("/video/playback" in u for u, _i, _f in both.items):
        problems.append("show_videos_in_timeline=true still hid videos")
    return problems


@case("setting timeout and ignore_ssl_errors reach ImmichClient")
def setting_client_transport(h):
    problems = []
    for timeout, ignore_ssl in ((5, False), (45, True), (120, False)):
        h.reset(timeout=timeout, ignore_ssl_errors=ignore_ssl)
        before = len(harness.CLIENT_INITS)
        h.invoke("action=timeline")
        made = harness.CLIENT_INITS[before:]
        if not made:
            problems.append("no ImmichClient was constructed")
            continue
        got = made[0]
        if got["timeout"] != timeout:
            problems.append(
                f"timeout={timeout} reached the client as {got['timeout']!r}"
            )
        if got["verify_ssl"] is not (not ignore_ssl):
            problems.append(
                f"ignore_ssl_errors={ignore_ssl} produced "
                f"verify_ssl={got['verify_ssl']!r}"
            )

    # The schema minimum is 5; the code clamps below that independently.
    h.reset(timeout=1)
    before = len(harness.CLIENT_INITS)
    h.invoke("action=timeline")
    made = harness.CLIENT_INITS[before:]
    if made and made[0]["timeout"] < 5:
        problems.append(
            f"kodiutils.py:117 should clamp the timeout to 5s, got "
            f"{made[0]['timeout']}"
        )
    return problems


@case("setting page_size: the floor is enforced and the window matches")
def setting_page_size(h):
    problems = []
    h.reset(page_size=50)
    h.dataset.buckets = [{"timeBucket": "2026-08-01", "count": 120}]
    h.dataset.bucket_sizes = {"2026-08-01": 120}
    record = h.invoke("action=bucket&id=2026-08-01")
    problems += standard_checks(record, expect_content="images")
    assets = [u for u, i, folder in record.items if not folder]
    if len(assets) != 50:
        problems.append(f"page_size=50 emitted {len(assets)} assets")

    # Below the schema minimum the code must still not produce a tiny page.
    h.reset(page_size=10)
    h.dataset.buckets = [{"timeBucket": "2026-08-01", "count": 120}]
    h.dataset.bucket_sizes = {"2026-08-01": 120}
    record = h.invoke("action=bucket&id=2026-08-01")
    problems += standard_checks(record, expect_content="images")
    assets = [u for u, i, folder in record.items if not folder]
    if len(assets) != 50:
        problems.append(
            f"kodiutils.py:146 clamps page_size to a minimum of 50, but a stored "
            f"value of 10 produced {len(assets)} assets"
        )
    return problems


# ==========================================================================
# v2.0.1 regression lock-ins
#
# Each case here fails against v2.0.0. Sources: immich-api-reference.md for
# API shapes, kodi-api-reference.md for Kodi semantics.
# ==========================================================================


def _md5_namespace(base_url):
    from hashlib import md5

    return md5(base_url.encode("utf-8")).hexdigest()[:12]


def _window(key):
    return harness.STATE.window_properties.get((10000, key))


# ---------------------------------------------------------------- new route


@case("v2.0.1 route 'videos': a flat search listing, not a filtered timeline",
      route="videos")
def videos_route(h):
    h.reset()
    record = h.invoke("action=videos")
    problems = standard_checks(record)

    if not record.items:
        problems.append("the videos route produced nothing")
    for url, item, isfolder in record.items:
        if isfolder:
            problems.append(
                f"views.py:110 videos must be one flat listing, but it emitted "
                f"the folder {item.label!r}"
            )
        elif "/video/playback" not in url:
            problems.append(f"a still leaked into the videos listing: {url!r}")

    posts = [r for r in h.server.requests if r["path"].endswith("/search/metadata")]
    if not posts:
        problems.append("videos did not POST /api/search/metadata")
    elif posts[0]["body"].get("type") != "VIDEO":
        problems.append(
            f"the search body has no type=VIDEO filter: {posts[0]['body']}"
        )
    if any(r["path"].endswith("/timeline/buckets") for r in h.server.requests):
        problems.append(
            "videos still walks the timeline, which takes no asset-type filter "
            "(reference section 2), so photo-only months would open empty"
        )

    content = [value for _handle, value in record.content]
    if content != ["videos"]:
        problems.append(
            f"views.py:509 sets content from the legacy `video=1` param only, so "
            f"the dedicated videos route reports setContent({content[0]!r}) for a "
            f"listing that contains nothing but videos. Kodi picks the view mode, "
            f"the sort options and the info dialog from this, so a videos-only "
            f"listing is presented as pictures. Trigger: action=videos."
        )
    return problems


@case("v2.0.1 route 'videos': the legacy video=1 param still works for favourites")
def videos_legacy_param(h):
    h.reset()
    record = h.invoke("action=bucket&id=2026-08-01&video=1")
    problems = standard_checks(record, expect_content="videos")
    for url, _item, _f in record.items:
        if "/video/playback" not in url:
            problems.append(f"video=1 listing contains a still: {url!r}")
    if not record.items:
        problems.append("a saved video=1 favourite now lists nothing")
    return problems


# ------------------------------------------------------------ artwork floor


@case("v2.0.1 artwork: photo and video rows keep a bundled icon under the thumbnail")
def artwork_floor(h):
    h.reset()
    problems = []
    for query in ("action=bucket&id=2026-08-01", f"action=album&id={ALBUM_1}&title=x",
                  "action=random", "action=videos"):
        record = h.invoke(query)
        problems += standard_checks(record)
        if not record.items:
            problems.append(f"{query}: nothing emitted")
        for _url, item, _f in record.items:
            icon = item.art.get("icon", "")
            if icon.startswith(("http://", "https://")):
                problems.append(
                    f"{query}: {item.label!r} has a remote icon {icon!r}"
                )
            elif not os.path.isfile(icon):
                problems.append(f"{query}: icon {icon!r} is not a file on disk")
            elif os.path.dirname(icon) != MEDIA_DIR:
                problems.append(
                    f"{query}: icon {icon!r} is not one of the bundled media files"
                )
            # The real picture must still be reachable.
            for key in ("thumb", "poster"):
                if "/api/assets/" not in item.art.get(key, ""):
                    problems.append(
                        f"{query}: {item.label!r} art[{key}] is not the remote "
                        f"thumbnail: {item.art.get(key)!r}"
                    )
    return problems


# ------------------------------------------- mimetype / content-lookup pair


@case("v2.0.1 mimetype: content lookup is disabled only when a mimetype is set")
def mimetype_pairing(h):
    h.reset()
    problems = []

    # A timeline bucket carries no mimetype (reference section 3), so neither
    # call may happen or Kodi is left unable to identify the stream.
    bucket = h.invoke("action=bucket&id=2026-08-01")
    problems += standard_checks(bucket, expect_content="images")
    for _url, item, _f in bucket.items:
        if item.mimetype:
            problems.append(
                f"a bucket asset has mimetype {item.mimetype!r}, which the "
                f"columnar bucket cannot supply"
            )
        if item.contentlookup is False:
            problems.append(
                f"listing.py disabled content lookup on {item.label!r} with no "
                f"mimetype, so Kodi has neither a HEAD probe nor a type hint"
            )

    # An album carries full DTOs, so both must happen.
    album = h.invoke(f"action=album&id={ALBUM_1}&title=x")
    problems += standard_checks(album, expect_content="images")
    seen = 0
    for _url, item, _f in album.items:
        if not item.mimetype:
            problems.append(f"album asset {item.label!r} lost its mimetype")
            continue
        seen += 1
        if item.contentlookup is not False:
            problems.append(
                f"album asset {item.label!r} has a mimetype but left the HEAD "
                f"probe enabled"
            )
    if not seen:
        problems.append("no album asset carried a mimetype at all")
    return problems


@case("v2.0.1 mimetype: a still with a video/* container is not tagged as video")
def mimetype_motion_photo(h):
    h.reset()
    h.dataset.add_motion_photo(ALBUM_1)
    record = h.invoke(f"action=album&id={ALBUM_1}&title=x")
    problems = standard_checks(record, expect_content="images")

    motion = [i for _u, i, _f in record.items if i.label.startswith("MVIMG_")
              or i.getProperty("immich_id").endswith("777-0000-4000-8000-000000000000")]
    if not motion:
        motion = [i for _u, i, _f in record.items
                  if "0000777" in i.getProperty("immich_id")]
    if not motion:
        problems.append("the motion photo never reached the listing")
        return problems
    item = motion[0]
    if item.mimetype:
        problems.append(
            f"listing.py:248 must not pass a video/* mimetype to a still: "
            f"setMimeType({item.mimetype!r}) makes VIDEO::IsVideo true, so Kodi "
            f"classifies an Android motion photo as a video"
        )
    if item.video_tag_requested:
        problems.append("the motion photo was given a video info tag")
    if item.contentlookup is False:
        problems.append(
            "content lookup was disabled on the motion photo without a mimetype"
        )
    return problems


# ------------------------------------------------------- video gate scoping


@case("v2.0.1 video gate: show_videos_in_timeline only filters the plain timeline")
def video_gate_scoping(h):
    problems = []
    named = (
        ("action=bucket&id=2026-08-01&favorite=1", "favourites"),
        (f"action=bucket&id=2026-08-01&personId={PERSON_1}", "a person"),
        (f"action=bucket&id=2026-08-01&tagId={TAG_1}", "a tag"),
        (f"action=bucket&id=2026-08-01&albumId={ALBUM_1}", "an album"),
    )
    h.reset(show_videos_in_timeline=False)

    plain = h.invoke("action=bucket&id=2026-08-01")
    problems += standard_checks(plain, expect_content="images")
    if any("/video/playback" in u for u, _i, _f in plain.items):
        problems.append("the plain timeline still shows videos when the setting is off")
    if not plain.items:
        problems.append("hiding videos emptied the plain timeline")

    for query, what in named:
        record = h.invoke(query)
        problems += standard_checks(record, expect_content="images")
        if not any("/video/playback" in u for u, _i, _f in record.items):
            problems.append(
                f"views.py:183 must treat {what} as a listing the user asked for "
                f"by name, but show_videos_in_timeline=false removed its videos "
                f"({query})"
            )
    return problems


# ------------------------------------------------------------- 403 vs 401


@case("v2.0.1 auth: 403 blames the scope, 401 blames the key")
def auth_403_vs_401(h):
    problems = []
    for status, expected_id in ((401, 30010), (403, 30087)):
        h.reset()
        h.dataset.force_status["/timeline"] = (status, "nope")
        record = h.invoke("action=timeline")
        problems += standard_checks(
            record, expect_succeeded=False, expect_content=None, allow_dialog=True
        )
        oks = [d for d in record.dialogs if d[0] == "ok"]
        if not oks:
            problems.append(f"HTTP {status} produced no dialog")
            continue
        heading, message = oks[0][1], oks[0][2]
        if heading != STRINGS[30009]:
            problems.append(f"HTTP {status}: heading {heading!r}")
        if message != STRINGS[expected_id]:
            problems.append(
                f"HTTP {status}: message {message!r}, expected string "
                f"{expected_id} {STRINGS[expected_id]!r}"
            )
    return problems


# ------------------------------------------------------------ retry policy


@case("v2.0.1 retry: a timeout is issued exactly once, never replayed")
def retry_no_timeout_replay(h):
    h.reset(timeout=5)
    h.dataset.hang_seconds = 8.0
    h.dataset.hang_paths.add("/timeline/buckets")
    record = h.invoke("action=timeline")
    problems = standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    hits = [r for r in h.server.requests if r["path"].endswith("/timeline/buckets")]
    if len(hits) != 1:
        problems.append(
            f"api.py:241 must not retry a timeout: the server may already have "
            f"processed the request, and re-sending doubles how long the Kodi UI "
            f"stays frozen. Got {len(hits)} requests, expected 1"
        )
    return problems


@case("v2.0.1 retry: a dropped keep-alive on a GET is replayed exactly once")
def retry_stale_connection(h):
    h.reset()
    h.dataset.drop_once.add("/timeline/buckets")
    record = h.invoke("action=timeline")
    problems = standard_checks(record, expect_content="files")
    # The mock records the request line before dropping the socket, so a
    # successful single retry is two arrivals: the dropped one and the replay.
    hits = [r for r in h.server.requests if r["path"].endswith("/timeline/buckets")]
    if len(hits) != 2:
        problems.append(
            f"a dropped keep-alive socket must be replayed exactly once on a "
            f"fresh connection; the server saw {len(hits)} arrivals, expected 2"
        )
    if not record.items:
        problems.append(
            "api.py:241 _is_stale_connection must treat RemoteDisconnected as "
            "retryable; the listing did not recover"
        )
    return problems


@case("v2.0.1 retry: a dropped keep-alive on a read-only POST is replayed")
def retry_search_post(h):
    h.reset()
    h.dataset.drop_once.add("/search/metadata")
    record = h.invoke("action=place&city=Amsterdam&title=Amsterdam")
    problems = standard_checks(record, expect_content="images")
    if not record.items:
        problems.append(
            "api.py:228 allows POST /search/* to be replayed because it is "
            "read-only, but the listing did not recover"
        )
    return problems


# --------------------------------------------------- connection diagnostics


@case("v2.0.1 connection errors: the dialog explains the failure")
def connection_diagnostics(h):
    import socket

    problems = []
    generic = STRINGS[30008]

    def dialog_of(record):
        oks = [d for d in record.dialogs if d[0] == "ok"]
        return oks[0] if oks else None

    # Refused
    h.reset()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
    probe.close()
    h.set_setting("immich_url", f"http://127.0.0.1:{dead}")
    record = h.invoke("action=timeline")
    problems += standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    entry = dialog_of(record)
    if entry is None:
        problems.append("a refused connection produced no dialog")
    else:
        if entry[1] != STRINGS[30007]:
            problems.append(f"refused: heading {entry[1]!r}")
        if entry[2] == generic or "refused" not in entry[2].lower():
            problems.append(
                f"api.py:267 should name the failure; refused connection showed "
                f"{entry[2]!r}"
            )

    # TLS: point https:// at the plain-HTTP mock.
    h.reset()
    h.set_setting("immich_url", h.server.url.replace("http://", "https://"))
    record = h.invoke("action=timeline")
    problems += standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    entry = dialog_of(record)
    if entry is None:
        problems.append("a TLS failure produced no dialog")
    elif "TLS" not in entry[2]:
        problems.append(f"TLS failure showed {entry[2]!r}, expected TLS advice")

    # Timeout
    h.reset(timeout=5)
    h.dataset.hang_seconds = 8.0
    h.dataset.hang_paths.add("/server/version")
    record = h.invoke("action=timeline")
    problems += standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    entry = dialog_of(record)
    if entry is None:
        problems.append("a timeout produced no dialog")
    elif "respond in time" not in entry[2]:
        problems.append(f"timeout showed {entry[2]!r}, expected timeout advice")
    return problems


# -------------------------------------------------------------------- dates


@case("v2.0.1 dates: localDateTime, dateTimeOriginal and a UTC instant + timeZone")
def date_resolution(h):
    from mockimmich import asset_dto

    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Asia/Tokyo")
        tz_available = True
    except Exception:  # noqa: BLE001
        tz_available = False

    h.reset()
    # 1: localDateTime wins, and is already local wall clock despite the Z.
    first = asset_dto(900)
    first["localDateTime"] = "2026-03-04T18:30:00.000Z"
    first["fileCreatedAt"] = "2026-03-04T09:00:00.000Z"
    first["exifInfo"]["dateTimeOriginal"] = "2026-03-04T07:00:00.000Z"

    # 2: no localDateTime, EXIF dateTimeOriginal is also local wall clock.
    second = asset_dto(901)
    second.pop("localDateTime")
    second["fileCreatedAt"] = "2026-03-05T22:00:00.000Z"
    second["exifInfo"]["dateTimeOriginal"] = "2026-03-05T07:15:00.000Z"

    # 3: only a UTC instant, plus the IANA zone Immich recorded.
    third = asset_dto(902)
    third.pop("localDateTime")
    third["fileCreatedAt"] = "2026-03-06T12:00:00.000Z"
    third["exifInfo"].pop("dateTimeOriginal")
    third["exifInfo"]["timeZone"] = "Asia/Tokyo"

    h.dataset.random_results = [first, second, third]
    record = h.invoke("action=random")
    problems = standard_checks(record, expect_content="images")
    labels = [i.label for _u, i, _f in record.items]
    if len(labels) != 3:
        problems.append(f"expected 3 assets, got {labels}")
        return problems

    if "18:30:00" not in labels[0]:
        problems.append(
            f"localDateTime must be read as local wall clock, not converted: "
            f"{labels[0]!r} should carry 18:30:00"
        )
    if "07:15:00" not in labels[1]:
        problems.append(
            f"exifInfo.dateTimeOriginal must be read as local wall clock: "
            f"{labels[1]!r} should carry 07:15:00"
        )
    expected = "21:00:00" if tz_available else "12:00:00"
    if expected not in labels[2]:
        problems.append(
            f"api.py:195 _utc_to_naive should turn the UTC instant 12:00:00 into "
            f"{expected} using exifInfo.timeZone=Asia/Tokyo "
            f"(zoneinfo {'available' if tz_available else 'unavailable'}); got "
            f"{labels[2]!r}"
        )
    return problems


# --------------------------------------------------------------- hardening


@case("v2.0.1 hardening: malformed server responses stay inside ImmichError")
def hardening_malformed(h):
    problems = []
    cases = [
        ("/server/version returning an empty body",
         {"/server/version": (200, b"")}, "action=timeline", False),
        ("/server/version returning a non-dict",
         {"/server/version": (200, b'["not", "a", "dict"]')}, "action=timeline", False),
        (f"/albums/{{id}} returning an empty body",
         {f"/albums/{ALBUM_1}": (200, b"")},
         f"action=album&id={ALBUM_1}&title=x", True),
    ]
    for label, override, query, succeeds in cases:
        h.reset()
        h.dataset.raw_override.update(override)
        record = h.invoke(query)
        problems += standard_checks(
            record, expect_succeeded=succeeds, expect_content=None, allow_dialog=True
        )
        if record.exception is not None:
            problems.append(
                f"{label}: escaped as {record.exception!r}, which dispatch cannot "
                f"turn into a dialog"
            )
    return problems


@case("v2.0.1 hardening: an asset with no id and a non-numeric localOffsetHours")
def hardening_bad_assets(h):
    from mockimmich import asset_dto

    problems = []

    h.reset()
    headless = asset_dto(903)
    headless.pop("id")
    h.dataset.random_results = [headless, asset_dto(904)]
    record = h.invoke("action=random")
    if record.exception is not None:
        problems.append(
            f"api.py:570 must tolerate an asset dict with no id, got "
            f"{record.exception!r}"
        )
    if len(record.end_of_directory) != 1:
        problems.append("an id-less asset broke the directory")

    h.reset()
    h.dataset.offset_hours = "not-a-number"
    record = h.invoke("action=bucket&id=2026-08-01")
    problems += standard_checks(record, expect_content="images")
    if record.exception is not None:
        problems.append(
            f"api.py:628 must tolerate a non-numeric localOffsetHours, got "
            f"{record.exception!r}"
        )
    if len(record.items) != 12:
        problems.append(
            f"a bad offset column cost {12 - len(record.items)} assets"
        )
    return problems


# ---------------------------------------------------------- path escaping


@case("v2.0.1 escaping: a traversal id cannot reach another endpoint")
def path_escaping(h):
    h.reset()
    record = h.invoke("action=album&id=..%2F..%2Fusers%2Fme&title=x")
    problems = standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    for request in h.server.requests:
        if request["path"].endswith("/api/users/me"):
            problems.append(
                "api.py:218 _segment must percent-encode an id before it becomes "
                "a path segment. `?action=album&id=../../users/me` reached "
                f"{request['path']} with the x-api-key header attached; plugin "
                "URLs are constructible by favourites, .strm files and other "
                "addons."
            )
        if "/../" in request["path"] or request["path"].endswith("/.."):
            problems.append(f"an unescaped traversal reached {request['path']!r}")
    if record.exception is not None:
        problems.append(f"unhandled exception: {record.exception!r}")
    return problems


# ------------------------------------------------------------ session cache


@case("v2.0.1 cache: an unparseable cached version is re-probed")
def cache_bad_version(h):
    h.reset()
    key = f"immich.server.version.{_md5_namespace(h.server.url)}"
    harness.STATE.window_properties[(10000, key)] = "not-a-version"
    record = h.invoke("action=timeline")
    problems = standard_checks(record, expect_content="files")
    probes = [r for r in h.server.requests if r["path"].endswith("/server/version")]
    if not probes:
        problems.append(
            "api.py:461 Version.parse yields 0.0.0 for anything unreadable. A "
            "cached 'not-a-version' must be discarded and re-probed, or every "
            "version gate silently takes the pre-1.118 path"
        )
    if _window(key) != "2.7.5":
        problems.append(f"the cache was not repaired: {_window(key)!r}")
    return problems


@case("v2.0.1 cache: a failed features probe is not cached as empty")
def cache_features_failure(h):
    h.reset()
    key = f"immich.server.features.{_md5_namespace(h.server.url)}"
    h.dataset.force_status["/server/features"] = (500, "boom")
    record = h.invoke("")
    problems = standard_checks(record, expect_content="files")
    if _window(key) is not None:
        problems.append(
            f"api.py:496-504 must not persist a failed feature probe: caching "
            f"{_window(key)!r} would disable feature gating for the rest of the "
            f"Kodi session after one dropped packet"
        )
    # Failing open: the menu must still be complete.
    labels = [i.label for _u, i, _f in record.items]
    if STRINGS[30053] not in labels:
        problems.append(f"a failed probe hid a menu entry: {labels}")

    # A later successful probe caches the real answer.
    h.dataset.force_status.pop("/server/features")
    record = h.invoke("")
    problems += standard_checks(record, expect_content="files")
    cached = _window(key)
    if not cached:
        problems.append("a successful feature probe was not cached")
    else:
        import json

        if json.loads(cached).get("smartSearch") is not True:
            problems.append(f"the cached features look wrong: {cached!r}")
    return problems


# ----------------------------------------------------------------- counts


@case("v2.0.1 counts: a null assetCount or bucket count does not kill the listing")
def null_counts(h):
    problems = []

    h.reset()
    h.dataset.albums[0]["assetCount"] = None
    h.dataset.albums[1]["assetCount"] = "lots"
    record = h.invoke("action=albums")
    problems += standard_checks(record, expect_content="files")
    if len(record.items) != 2:
        problems.append(
            f"views.py:44 _count must coerce a null assetCount; the listing lost "
            f"{2 - len(record.items)} albums"
        )
    for _u, item, _f in record.items:
        if "None" in item.label2 or "%d" in item.label2:
            problems.append(f"album label2 is {item.label2!r}")

    h.reset()
    h.dataset.buckets = [
        {"timeBucket": "2026-08-01", "count": None},
        {"timeBucket": "2026-07-01"},
    ]
    h.dataset.bucket_sizes = {"2026-08-01": 12, "2026-07-01": 3}
    record = h.invoke("action=timeline")
    problems += standard_checks(record, expect_content="files")
    if len(record.items) != 2:
        problems.append(
            f"a null bucket count cost {2 - len(record.items)} months"
        )
    return problems


# -------------------------------------------------------- slideshow target


@case("v2.0.1 slideshow: a foreign target is refused")
def slideshow_foreign_target(h):
    from urllib.parse import quote

    h.reset()
    problems = []
    for target in ("smb://server/share", "special://home/userdata",
                   "plugin://plugin.video.other/?action=x", "/etc"):
        record = h.invoke(
            f"action=slideshow&target={quote(target, safe='')}", handle=-1
        )
        problems += standard_checks(
            record, expect_directory=False, expect_content=None, allow_dialog=True
        )
        if record.builtins:
            problems.append(
                f"views.py:429 must refuse a target outside this addon; "
                f"target={target!r} ran {record.builtins[0]!r} under the addon's "
                f"own identity"
            )

    own = "plugin://plugin.video.immich/?action=timeline"
    record = h.invoke(
        f"action=slideshow&target={quote(own, safe='')}", handle=-1
    )
    problems += standard_checks(
        record, expect_directory=False, expect_content=None, allow_dialog=True
    )
    if len(record.builtins) != 1:
        problems.append(f"the addon's own target was refused: {record.builtins}")
    return problems


# ------------------------------------------------------- credential gate


@case("v2.0.1 credentials: settings and test_connection work before setup")
def credential_gate(h):
    problems = []
    not_configured = STRINGS[30050]

    # test_connection with no key must still run and report the real failure.
    h.reset(api_key="")
    record = h.invoke("action=test_connection", handle=-1)
    problems += standard_checks(
        record, expect_directory=False, expect_content=None, allow_dialog=True
    )
    if record.settings_opened:
        problems.append(
            "router.py:120 exempts test_connection from the credential gate, but "
            "it still forced the settings dialog open"
        )
    headings = [d[1] for d in record.dialogs if d[0] == "ok"]
    if not_configured in headings:
        problems.append(
            f"test_connection with a blank key showed 'Not configured' instead of "
            f"testing: {headings}"
        )
    if not headings:
        problems.append("test_connection reported nothing at all")

    # settings must open the dialog itself, not via the gate.
    h.reset(immich_url="")
    record = h.invoke("action=settings")
    problems += standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    if record.settings_opened != 1:
        problems.append(
            f"action=settings with a blank URL opened the dialog "
            f"{record.settings_opened} times, expected 1"
        )
    if any(d[1] == not_configured for d in record.dialogs if d[0] == "ok"):
        problems.append("action=settings showed 'Not configured' before opening")

    # Everything else still gates.
    h.reset(api_key="")
    record = h.invoke("action=timeline")
    problems += standard_checks(
        record, expect_succeeded=False, expect_content=None, allow_dialog=True
    )
    if not any(d[1] == not_configured for d in record.dialogs if d[0] == "ok"):
        problems.append("a listing route no longer gates on missing credentials")
    return problems


# ----------------------------------------------------------- month labels


@case("v2.0.1 labels: month names come from Kodi core strings 21-32")
def month_core_strings(h):
    h.reset()
    h.dataset.buckets = [
        {"timeBucket": "2026-06-01", "count": 3},
        {"timeBucket": "2025-12-01", "count": 1},
    ]
    h.dataset.bucket_sizes = {"2026-06-01": 3, "2025-12-01": 1}
    record = h.invoke("action=timeline")
    problems = standard_checks(record, expect_content="files")

    labels = [i.label for _u, i, _f in record.items]
    if labels != ["June 2026", "December 2025"]:
        problems.append(
            f"listing.py:62 should read month names from Kodi core strings "
            f"20+month, which are translated; got {labels}"
        )
    asked = [i for i, _found in record.core_string_requests]
    if 26 not in asked:
        problems.append(
            f"June should resolve through xbmc.getLocalizedString(26); the core "
            f"strings requested were {asked}"
        )
    for string_id in asked:
        if 30000 <= string_id <= 33999:
            problems.append(
                f"xbmc.getLocalizedString({string_id}) is an addon-range id "
                f"(reference: 30000-30999 plugins, 33000-33999 common); addon "
                f"strings must go through xbmcaddon.Addon().getLocalizedString"
            )
    return problems


@case("v2.0.1 labels: no route ever asks Kodi core for an addon string id")
def no_addon_ids_in_core(h):
    h.reset()
    problems = []
    for query in ("", "action=timeline", "action=bucket&id=2026-08-01",
                  "action=albums", f"action=album&id={ALBUM_1}", "action=videos",
                  "action=favourites", "action=people", "action=places",
                  "action=tags", "action=memories", "action=random",
                  "action=search"):
        record = h.invoke(query)
        for string_id, found in record.core_string_requests:
            if 30000 <= string_id <= 33999:
                problems.append(f"{query}: xbmc.getLocalizedString({string_id})")
            elif not found:
                problems.append(
                    f"{query}: xbmc.getLocalizedString({string_id}) is not a "
                    f"known Kodi core string"
                )
    return problems


# --------------------------------------------------------------- caching


@case("v2.0.1 caching: a successful listing is cached to disc")
def cache_to_disc(h):
    h.reset()
    problems = []
    for query in ("", "action=timeline", "action=bucket&id=2026-08-01",
                  "action=albums", "action=people", "action=videos"):
        record = h.invoke(query)
        if len(record.end_of_directory) != 1:
            problems.append(f"{query}: endOfDirectory not called exactly once")
            continue
        _handle, succeeded, _update, cache = record.end_of_directory[0]
        if not succeeded:
            problems.append(f"{query}: listing failed")
        elif cache is not True:
            problems.append(
                f"router.py:111 endOfDirectory should pass cacheToDisc=True so "
                f"Back is served from Kodi's cache instead of a round trip; "
                f"{query} passed {cache!r}"
            )
    return problems


# ------------------------------------------------------- settings schema


@case("v2.0.1 settings: the SSL, timeout and page size options are not hidden")
def settings_levels(h):
    problems = []
    for setting_id in ("ignore_ssl_errors", "timeout", "page_size"):
        schema = SETTINGS_SCHEMA.get(setting_id)
        if schema is None:
            problems.append(f"{setting_id} is not declared in settings.xml")
            continue
        if schema["level"] != 0:
            problems.append(
                f"resources/settings.xml: {setting_id} is <level>{schema['level']}"
                f"</level>, so it only appears once the user switches the "
                f"settings dialog to Advanced; expected 0"
            )
    # Nothing else should have regressed to a hidden level either.
    for setting_id, schema in SETTINGS_SCHEMA.items():
        if schema["level"] not in (0, 1, 2, 3):
            problems.append(f"{setting_id} has an invalid level {schema['level']}")
    return problems


# -------------------------------------------------------------- packaging


@case("v2.0.1 packaging: nothing reaches the lib modules by their package path")
def packaging_single_identity(h):
    """The stronger claim is unachievable, so assert the one that protects us.

    Deleting resources/lib/__init__.py does not give the modules a single
    import identity: PEP 420 makes `resources.lib` importable as a namespace
    package whenever the addon root is on sys.path, which Kodi guarantees for a
    pluginsource addon. Importing that way yields a second module object with
    its own empty route registry.

    Nothing can prevent that. What matters is that no shipped code takes that
    path, and that the flat registry dispatch reads is the populated one.
    """
    problems = []

    init = os.path.join(LIB, "__init__.py")
    if os.path.exists(init):
        problems.append(
            f"{init} exists, making resources/lib a regular package as well as "
            f"a namespace one. Nothing needs it."
        )

    # Parsed, not grepped: prose about the hazard is not the hazard, and the
    # source documents it deliberately.
    for path in _lib_sources():
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "resources" or name.startswith("resources."):
                    problems.append(
                        f"{path}:{node.lineno}: imports {name!r}. That is a "
                        f"second module object with an empty route registry; "
                        f"import the module flat instead."
                    )

    # And the flat registry, which dispatch actually reads, must be complete.
    code = (
        "import sys, json\n"
        f"sys.path[:0] = [{STUBS!r}, {LIB!r}]\n"
        "import router, views\n"
        "print(json.dumps(sorted(router._ROUTES)))"
    )
    finished = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    if finished.returncode != 0:
        problems.append(f"probe failed: {finished.stderr.strip()}")
        return problems
    import json as _json

    registered = _json.loads(finished.stdout.strip().splitlines()[-1])
    declared = {action for action, _name, _line in discover_routes()}
    missing = declared - set(registered)
    if missing:
        problems.append(f"the flat route registry is missing {sorted(missing)}")
    return problems


# ========================================================================
# Paging cost (see tests/bench.py for the measurements these lock in)
# ========================================================================


def _search_posts(h):
    return [r for r in h.server.requests if r["path"].endswith("/search/metadata")]


@case("perf: videos fetches one page, not the whole result set")
def perf_videos_single_page(h):
    """search_metadata used to walk every page and slice out one.

    Measured on a 2000-video library: 3.2 MB downloaded to render 500 items,
    and the whole walk repeated for page two. Server-side paging makes the cost
    constant per page.
    """
    h.reset(page_size=50)
    record = h.invoke("action=videos&page=2")
    problems = standard_checks(record)

    posts = _search_posts(h)
    if len(posts) != 1:
        problems.append(
            f"rendering one page issued {len(posts)} search requests; it must "
            f"fetch only the page being shown"
        )
    for body in (p["body"] for p in posts):
        if body.get("page") != 3:
            problems.append(f"asked for page {body.get('page')!r}, expected 3")
        if body.get("size") != 50:
            problems.append(f"asked for size {body.get('size')!r}, expected 50")
        if body.get("type") != "VIDEO":
            problems.append(f"lost the type filter: {body}")
    if len(record.items) > 51:
        problems.append(
            f"emitted {len(record.items)} items for a 50-item page, so the "
            f"whole result set is still being materialised"
        )
    return problems


@case("perf: an album page does not re-download the whole album")
def perf_album_single_page(h):
    """GET /albums/{id} embeds every asset with no paging.

    Measured on a 3000-asset album: 4.5 MB and ~100 ms per page, identical on
    page six as on page one.
    """
    h.reset(page_size=50)
    record = h.invoke(f"action=album&id={ALBUM_1}&title=x&order=asc&page=5")
    problems = standard_checks(record)

    if any(r["path"].endswith(f"/albums/{ALBUM_1}") for r in h.server.requests):
        problems.append(
            "the album was re-fetched through GET /api/albums/{id}, which "
            "returns every asset regardless of the page being rendered"
        )
    posts = _search_posts(h)
    if len(posts) != 1:
        problems.append(f"expected one paged request, got {len(posts)}")
    for body in (p["body"] for p in posts):
        if body.get("page") != 6:
            problems.append(f"asked for page {body.get('page')!r}, expected 6")
        if body.get("size") != 50:
            problems.append(f"asked for size {body.get('size')!r}, expected 50")
    return problems


@case("perf: a malformed album id is rejected before any request is sent")
def perf_album_id_validated(h):
    """Client-side, so it holds whether the id travels as a path or a body key.

    Without it the addon relies on the server to reject the value, which costs
    a round trip and depends on the server validating at all.
    """
    h.reset()
    record = h.invoke("action=album&id=not-a-uuid&title=x")
    problems = []
    if record.exception is not None:
        problems.append(f"unhandled exception: {record.exception!r}")
    if _search_posts(h):
        problems.append(
            "a malformed album id still reached the server; it must be "
            "rejected client-side"
        )
    if record.end_of_directory and record.end_of_directory[0][1]:
        problems.append("a malformed album id produced a successful listing")
    return problems


@case("setting video_playback: transcode by default, original on request")
def setting_video_playback(h):
    """Raspberry Pi 5 has a 4Kp60 HEVC decoder and no hardware H.264 decoder.

    Immich's default policy accepts H.264 only and re-encodes everything else
    to H.264 720p, so on that board the transcode is both software-decoded and
    downscaled while the original would be neither.
    """
    problems = []

    h.reset(video_playback=0)
    record = h.invoke("action=videos")
    urls = [u for u, _i, _f in record.items]
    if not urls:
        problems.append("no videos listed under the default setting")
    for url in urls:
        if "/video/playback" not in url:
            problems.append(f"default must use the transcode endpoint: {url!r}")

    h.reset(video_playback=1)
    record = h.invoke("action=videos")
    urls = [u for u, _i, _f in record.items]
    if not urls:
        problems.append("no videos listed with video_playback=1")
    for url in urls:
        if "/original" not in url:
            problems.append(f"video_playback=1 must serve the original: {url!r}")
        if "/video/playback" in url:
            problems.append(f"video_playback=1 still hit the transcode: {url!r}")

    # Stills must be unaffected either way.
    h.reset(video_playback=1)
    record = h.invoke("action=bucket&id=2026-08-01")
    for url, item, _f in record.items:
        if item.video_tag_requested:
            continue
        if "/original" in url and h.settings_value("image_quality") == 0:
            problems.append(f"a still was switched to /original by a video setting: {url!r}")
    return problems


@case("regression: the addon is listed under Pictures only")
def provides_pictures_only(h):
    """Reported on a Raspberry Pi 5: clicking any photo did nothing.

    CGUIWindowVideoBase has no image handling — OnFileAction sends a non-folder
    item straight to PlayItem(), and the video player fails on a JPEG. Only
    CGUIWindowPictures can display a still, and it plays videos too, so one
    section serves both. Declaring `video` in <provides> adds a second entry
    point where every photo is broken.
    """
    import xml.etree.ElementTree as ET

    problems = []
    root = ET.parse(os.path.join(REPO, "addon.xml")).getroot()
    provides = (root.findtext(".//provides") or "").split()
    if "video" in provides:
        problems.append(
            "addon.xml declares <provides>video</provides>, which lists the "
            "addon under Videos where CGUIWindowVideoBase cannot display a "
            "still; every photo fails there"
        )
    if provides != ["image"]:
        problems.append(f"expected <provides>image</provides>, got {provides}")

    # A still must still be classified as a picture: CFileItem::IsPicture
    # returns true on HasPictureInfoTag, which getPictureInfoTag creates.
    h.reset()
    record = h.invoke("action=bucket&id=2026-08-01")
    stills = [i for u, i, _f in record.items if "/video/playback" not in u]
    if not stills:
        problems.append("no stills in the bucket listing to check")
    for item in stills:
        if not item.picture_tag_requested:
            problems.append(
                f"still {item.label!r} has no picture info tag, so "
                f"CFileItem::IsPicture is false and ShowPicture skips it"
            )
    return problems


@case("route 'all': one flat chronological listing, no folders", route="all")
def route_all_media(h):
    """Kodi builds a slideshow from the current directory only.

    CGUIWindowPictures::ShowPicture iterates m_vecItems, so next and previous
    can never leave the folder. Month folders therefore always stop at the end
    of the month; this listing has no folders, so a page flows across months.
    """
    h.reset(page_size=50)
    record = h.invoke("action=all")
    problems = standard_checks(record)

    if not record.items:
        problems.append("the flat listing produced nothing")
    folders = [i.label for _u, i, f in record.items if f and i.label != "Next page"]
    if folders:
        problems.append(f"the flat listing emitted folders: {folders}")

    posts = _search_posts(h)
    if len(posts) != 1:
        problems.append(f"expected one paged request, got {len(posts)}")
    for body in (p["body"] for p in posts):
        if body.get("order") != "desc":
            problems.append(f"not newest-first: order={body.get('order')!r}")
        if body.get("size") != 50:
            problems.append(f"page size not honoured: {body.get('size')!r}")

    # Assets from more than one month must be able to sit in one listing, or
    # the whole point of the route is lost.
    dates = {
        i.getProperty("immich_id")[:1] for _u, i, f in record.items if not f
    }
    if not dates:
        problems.append("no asset items in the flat listing")
    return problems


@case("setting month_previews: off by default, and costs one request per month")
def setting_month_previews(h):
    """Measured on a 120-month library: 2 requests becomes 122.

    The buckets endpoint returns only {timeBucket, count} and Immich has no
    per-bucket cover image, so a preview means a request per month. Opt-in.
    """
    problems = []

    h.reset(month_previews=False)
    record = h.invoke("action=timeline")
    baseline = len(h.server.requests)
    months = len([1 for _u, _i, f in record.items if f])
    if baseline > 3:
        problems.append(
            f"the default timeline issued {baseline} requests for {months} "
            f"months; previews must be off unless asked for"
        )
    for _u, item, _f in record.items:
        icon = item.getArt("icon")
        if icon and icon.startswith(("http://", "https://")):
            problems.append(f"month {item.label!r} art points at the network by default")

    h.reset(month_previews=True)
    record = h.invoke("action=timeline")
    withpreview = len(h.server.requests)
    months = len([1 for _u, _i, f in record.items if f])
    if withpreview < baseline + months:
        problems.append(
            f"month_previews=True issued {withpreview} requests for {months} "
            f"months; each month needs its own lookup"
        )
    thumbs = [
        i.getArt("thumb") for _u, i, f in record.items if f
    ]
    if not any(t.startswith(("http://", "https://")) for t in thumbs if t):
        problems.append("no month got a remote preview thumbnail")
    # The bundled icon must remain the floor even with previews on.
    for _u, item, f in record.items:
        if f and item.getArt("icon").startswith(("http://", "https://")):
            problems.append(f"month {item.label!r} lost its bundled icon fallback")
    return problems


@case("regression: Test connection names a missing asset.download scope")
def scope_reporting(h):
    """Reported from real use: Original playback gave only "playback failed".

    /assets/{id}/original needs asset.download; the transcode and thumbnails
    need only asset.view. Kodi's dialog cannot know that, so the addon has to
    say it.
    """
    problems = []

    # Scope present: no warning.
    h.reset(video_playback=1)
    h.invoke("action=test_connection", handle=-1)
    oks = [d for d in harness.STATE.dialogs if d[0] == "ok"]
    if oks and localise_text(30096) in oks[0][2]:
        problems.append("warned about a scope the key actually has")

    # Scope absent while Original is selected: must name it.
    h.reset(video_playback=1)
    h.dataset.key_permissions = ["timeline.read", "asset.read", "asset.view"]
    h.invoke("action=test_connection", handle=-1)
    oks = [d for d in harness.STATE.dialogs if d[0] == "ok"]
    if not oks:
        problems.append("test_connection showed no dialog at all")
    elif localise_text(30096) not in oks[0][2]:
        problems.append(
            f"a missing asset.download scope was not reported while Original "
            f"playback is selected: {oks[0][2]!r}"
        )

    # Scope absent but Transcoded selected: a milder note, still mentioned.
    h.reset(video_playback=0)
    h.dataset.key_permissions = ["timeline.read", "asset.read", "asset.view"]
    h.invoke("action=test_connection", handle=-1)
    oks = [d for d in harness.STATE.dialogs if d[0] == "ok"]
    if oks and localise_text(30097) not in oks[0][2]:
        problems.append(f"missing scope not mentioned at all: {oks[0][2]!r}")
    return problems


def localise_text(string_id):
    return STRINGS.get(string_id, f"<missing {string_id}>")


# --------------------------------------------------------------- addon.xml


@case("v2.0.1 metadata: version, licence and news agree with the tree")
def addon_metadata(h):
    import xml.etree.ElementTree as ET

    problems = []
    root = ET.parse(os.path.join(REPO, "addon.xml")).getroot()
    version = root.get("version") or ""
    # Derived, not pinned: pinning means every release edits a test.
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        problems.append(f"addon.xml version {version!r} is not a release triple")

    # Every v20 API this addon uses (getPictureInfoTag, ListItem.setDateTime,
    # the InfoTagVideo setters) needs Kodi 20. Kodi 19 ships xbmc.python 3.0.0
    # and Kodi 20 ships 3.0.1, so declaring 3.0.0 lets Kodi 19 install the
    # addon and then fail on the first listing.
    imports = {
        node.get("addon"): node.get("version")
        for node in root.findall(".//requires/import")
    }
    if imports.get("xbmc.python") != "3.0.1":
        problems.append(
            f"addon.xml requires xbmc.python {imports.get('xbmc.python')!r}; "
            f"'3.0.1' is the Kodi 20 floor this addon's APIs need"
        )

    licence = (root.findtext(".//license") or "").strip()
    if licence != "GPL-3.0-or-later":
        problems.append(f"addon.xml licence is {licence!r}")
    licence_text = open(os.path.join(REPO, "LICENSE.txt"), encoding="utf-8").read()
    if "GNU GENERAL PUBLIC LICENSE" not in licence_text.upper():
        problems.append("LICENSE.txt is not the GPL text")
    if "Version 3" not in licence_text[:400]:
        problems.append("LICENSE.txt is not GPL version 3")
    if licence.startswith("GPL-3") and "MIT" in licence:
        problems.append("licence tag disagrees with LICENSE.txt")

    news = (root.findtext(".//news") or "").strip()
    if not news.startswith(f"v{version}"):
        problems.append(
            f"addon.xml <news> starts with {news.splitlines()[0]!r}, which does "
            f"not match the version attribute {version!r}"
        )

    # Kodi reads the addon id from here and keys userdata by it.
    if root.get("id") != "plugin.video.immich":
        problems.append(f"the addon id changed to {root.get('id')!r}")
    return problems
