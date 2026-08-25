"""Build the fixed team-recommendation catalog from the Resonance BWIKI."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    REPO_ROOT / "plans" / "resonance_pc" / "data" / "meta" / "team_recommendations.json"
)
CHARACTER_ROOT = REPO_ROOT / "plans" / "resonance_pc" / "templates" / "characters"
SOURCE_URL = "https://wiki.biligame.com/resonance/%E9%85%8D%E9%98%9F%E6%94%BB%E7%95%A5"
API_URL = "https://wiki.biligame.com/resonance/api.php"
USER_AGENT = "AuraResonance-TeamCatalog/1.0"
TARGET_TEMPLATE = re.compile(r"\{\{(娃娃鱼报社配队攻略(?:-[234])?)\s*\|")
COMMENT = re.compile(r"<!--[\s\S]*?-->")
SECTION_SEPARATOR = re.compile(r"[,，、\s]+")


def _clean(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _page_title(value: object) -> str:
    """Preserve MediaWiki-significant full-width punctuation in page titles."""

    return str(value or "").strip()


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed trusted host
        return response.read().decode("utf-8")


def _fetch_json(params: dict[str, str]) -> dict:
    request = Request(
        f"{API_URL}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed trusted host
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("error"):
        raise RuntimeError(f"BWIKI API request failed: {payload!r}")
    return payload


class _TeamCardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, object]] = []
        self._card: dict[str, object] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "div" and "pic-button-team" in classes and self._card is None:
            self._card = {
                "title": "",
                "href": "",
                "sections": [
                    value
                    for value in SECTION_SEPARATOR.split(values.get("data-param1", ""))
                    if value
                ],
                "members": [
                    _clean(value)
                    for value in values.get("data-param2", "").split(",")
                    if _clean(value)
                ],
            }
            self._depth = 1
            return
        if self._card is None:
            return
        if tag == "div":
            self._depth += 1
        if tag == "a" and not self._card["title"] and values.get("title"):
            self._card["title"] = _page_title(values["title"])
            self._card["href"] = values.get("href", "")

    def handle_endtag(self, tag: str) -> None:
        if self._card is None or tag != "div":
            return
        self._depth -= 1
        if self._depth == 0:
            if self._card["title"]:
                self.cards.append(self._card)
            self._card = None


def _load_team_cards() -> tuple[list[dict[str, object]], int]:
    parser = _TeamCardParser()
    parser.feed(_fetch_text(SOURCE_URL))
    parse_payload = _fetch_json(
        {
            "action": "parse",
            "page": "配队攻略",
            "prop": "revid",
            "format": "json",
            "formatversion": "2",
        }
    )
    revision_id = int(parse_payload["parse"]["revid"])
    return parser.cards, revision_id


def _load_page_sources(titles: list[str]) -> dict[str, dict[str, object]]:
    contents: dict[str, dict[str, object]] = {}
    aliases: dict[str, str] = {}
    for offset in range(0, len(titles), 20):
        batch = titles[offset : offset + 20]
        payload = _fetch_json(
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content|ids",
                "rvslots": "main",
                "titles": "|".join(batch),
                "redirects": "1",
                "format": "json",
                "formatversion": "2",
            }
        )
        query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
        for row in [*(query.get("normalized") or []), *(query.get("redirects") or [])]:
            aliases[_page_title(row.get("from"))] = _page_title(row.get("to"))
        for page in query.get("pages") or []:
            revisions = page.get("revisions") if isinstance(page, dict) else None
            revision = revisions[0] if isinstance(revisions, list) and revisions else {}
            slots = revision.get("slots") if isinstance(revision, dict) else {}
            main = slots.get("main") if isinstance(slots, dict) else {}
            contents[_page_title(page.get("title"))] = {
                "wikitext": str(main.get("content") or ""),
                "revision_id": int(revision.get("revid") or 0),
            }
        time.sleep(0.15)

    def resolve(title: str) -> str:
        resolved = title
        for _ in range(5):
            if resolved not in aliases:
                break
            resolved = aliases[resolved]
        return resolved

    result: dict[str, dict[str, object]] = {}
    for title in titles:
        resolved = resolve(title)
        if resolved not in contents:
            raise RuntimeError(f"BWIKI page content is missing: {title}")
        result[title] = {**contents[resolved], "canonical_title": resolved}
    return result


def _extract_template(wikitext: str) -> tuple[str, dict[str, str]]:
    clean = COMMENT.sub("", wikitext)
    match = TARGET_TEMPLATE.search(clean)
    if match is None:
        raise ValueError("team guide template was not found")
    depth = 0
    end = len(clean)
    index = match.start()
    while index < len(clean) - 1:
        pair = clean[index : index + 2]
        if pair == "{{":
            depth += 1
            index += 2
            continue
        if pair == "}}":
            depth -= 1
            index += 2
            if depth == 0:
                end = index
                break
            continue
        index += 1
    block = clean[match.start() : end]
    params: dict[str, str] = {}
    for line in block.splitlines():
        row = re.match(r"^\|([^=]+)=(.*)$", line)
        if row:
            params[_clean(row.group(1))] = _clean(row.group(2))
    return match.group(1), params


def _awakening(value: str, *, required: bool) -> int | None:
    if value == "" and not required:
        return None
    if not re.fullmatch(r"[0-5]", value):
        raise ValueError(f"invalid awakening value: {value!r}")
    return int(value)


def _build_team(
    card: dict[str, object],
    source: dict[str, object],
    known_characters: set[str],
) -> dict[str, object]:
    title = _page_title(card["title"])
    try:
        template, params = _extract_template(str(source["wikitext"]))
    except ValueError as exc:
        raise ValueError(f"{title}: {exc}") from exc
    members: list[dict[str, object]] = []
    warnings: list[str] = []
    for slot in range(1, 6):
        character_id = _clean(params.get(f"角色{slot}"))
        if not character_id:
            raise ValueError(f"{title}: role {slot} has no character")
        if character_id not in known_characters:
            raise ValueError(f"{title}: unknown character {character_id}")
        minimum = _awakening(params.get(f"角色{slot}最低觉醒", ""), required=True)
        recommended = _awakening(
            params.get(f"角色{slot}推荐觉醒", ""), required=False
        )
        if recommended is not None and recommended < minimum:
            raise ValueError(
                f"{title}: recommended awakening is below minimum for {character_id}"
            )
        if recommended is None:
            warnings.append(f"{character_id}:recommended_awakening_missing")

        full_weapon = _clean(params.get(f"角色{slot}武器")) or None
        low_key = f"角色{slot}武器备选"
        if low_key not in params:
            low_weapon = full_weapon
            low_source = "template_fallback_full"
        else:
            low_weapon = _clean(params[low_key]) or None
            low_source = "explicit" if low_weapon else "explicit_blank"
        if full_weapon is None:
            warnings.append(f"{character_id}:full_weapon_missing")
        if low_weapon is None:
            warnings.append(f"{character_id}:low_weapon_missing")
        members.append(
            {
                "slot": slot,
                "character_id": character_id,
                "minimum_awakening": minimum,
                "recommended_awakening": recommended,
                "full_weapon_id": full_weapon,
                "low_weapon_id": low_weapon,
                "low_weapon_source": low_source,
            }
        )

    card_members = [_clean(value) for value in card.get("members") or []]
    parsed_members = [str(row["character_id"]) for row in members]
    if card_members and card_members != parsed_members:
        raise ValueError(
            f"{title}: main-page roster does not match guide: {card_members!r} != {parsed_members!r}"
        )
    categories = [_clean(value) for value in card.get("sections") or [] if _clean(value)]
    return {
        "team_id": title,
        "title": title,
        "guide_name": _clean(params.get("队伍名字")) or title,
        "categories": categories,
        "source_url": f"https://wiki.biligame.com/resonance/{quote(title)}",
        "source_revision_id": int(source["revision_id"]),
        "template": template,
        "members": members,
        "warnings": warnings,
    }


def build_catalog(*, expected_count: int = 123) -> dict[str, object]:
    cards, source_revision_id = _load_team_cards()
    if len(cards) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} team cards from BWIKI, found {len(cards)}"
        )
    titles = [_page_title(card["title"]) for card in cards]
    if len(titles) != len(set(titles)):
        raise RuntimeError("BWIKI team titles are not unique")
    sources = _load_page_sources(titles)
    known_characters = {path.name for path in CHARACTER_ROOT.iterdir() if path.is_dir()}
    teams = [
        _build_team(card, sources[_page_title(card["title"])], known_characters)
        for card in cards
    ]
    return {
        "schema_version": 1,
        "source": {
            "url": SOURCE_URL,
            "revision_id": source_revision_id,
            "retrieved_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        },
        "team_count": len(teams),
        "teams": teams,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-count", type=int, default=123)
    args = parser.parse_args()
    payload = build_catalog(expected_count=args.expected_count)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    warning_count = sum(len(team["warnings"]) for team in payload["teams"])
    print(
        json.dumps(
            {
                "output": str(output),
                "teams": payload["team_count"],
                "warnings": warning_count,
                "source_revision_id": payload["source"]["revision_id"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
