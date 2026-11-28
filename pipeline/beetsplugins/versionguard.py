# beets plugin: never tag a song as a different version of itself.
# Matching by sound (AcoustID) can't tell a studio recording from its "take 2", demo,
# live or remix version, and beets' title comparison barely notices "(take 2)", so a
# "Dock of the Bay [2020 Remaster]" download got tagged as an alternate take. This adds
# a "version" penalty to beets' track comparison whenever the downloaded title and the
# MusicBrainz title don't carry the same version words. The wrong version then loses to
# the right recording, or, when MusicBrainz offers no right one, the song keeps its
# YouTube tags (quiet_fallback: asis) instead of the wrong ones.
# beets 2.x has no plugin hook for track distance, so this wraps beets' own function.
# Loaded through pluginpath (config.yaml and the generated run-config.yaml).
import re

from beets import config
from beets.autotag import distance, match
from beets.plugins import BeetsPlugin

WEIGHT = 5.0   # a mismatch alone pushes a match far past match.strong_rec_thresh (0.10)

# Each script keeps its own version list (see CLAUDE.md). Left out on purpose: "edit"
# (the album version usually is the radio edit), "remaster" (same recording) and plain
# "version" ("Single Version", "Soundtrack Version" name the same performance).
WORDS = re.compile(r"\b(remix(?:ed)?|mix|live|acoustic|instrumental|extended|demo|cover|karaoke|"
                   r"a ?cappella|unplugged|reprise|slowed|sped up|nightcore|alternate|alt\.? take|"
                   r"take \d+|rehearsal|session)\b", re.I)
SAME = {"remixed": "remix", "mix": "remix", "acapella": "a cappella", "acappella": "a cappella",
        "alt take": "alternate", "alt. take": "alternate"}
PLAIN_MIX = re.compile(r"\b(original|album|radio|single|main|clean|dirty|explicit|stereo|mono)\s+mix\b", re.I)


def versions(title: str) -> set[str]:
    """The version words in a title, normalised: "(Remix)" and "[Club Mix]" are both
    "remix", "Take 2" is "take 2". "Original Mix"-style labels are no version."""
    t = PLAIN_MIX.sub(" ", title or "")
    out = set()
    for m in WORDS.finditer(t):
        w = " ".join(m.group(1).lower().split())
        out.add(SAME.get(w, w))
    return out


def differs(item_title: str, mb_title: str) -> bool:
    return versions(item_title) != versions(mb_title)


class VersionGuard(BeetsPlugin):
    def __init__(self) -> None:
        super().__init__()
        weights = config["match"]["distance_weights"]
        if "version" not in weights.keys():
            weights.add({"version": WEIGHT})   # lowest priority: a user's own weight wins
        original = match.track_distance
        if getattr(original, "_versionguard", False):   # loaded twice in one process
            return

        def track_distance(item, track_info, incl_artist=False):
            dist = original(item, track_info, incl_artist)
            dist.add_expr("version", differs(item.title, track_info.title or ""))
            return dist

        track_distance._versionguard = True
        match.track_distance = track_distance      # singletons (what the app imports)
        distance.track_distance = track_distance   # albums
