"""Index analyzer plugin for Yeti indicators."""

import json
import re

from typing import Dict, List

from flask import current_app
import requests

from timesketch.lib.analyzers import interface
from timesketch.lib.analyzers import manager
from timesketch.lib import emojis


TYPE_TO_EMOJI = {
    "malware": "SPIDER",
    "threat-actor": "SKULL",
    "intrusion-set": "SKULL",
    "tool": "HAMMER",
    "investigation": "FIRE",
    "campaign": "BOMB",
    "vulnerability": "SHIELD",
}

NEIGHBOR_CACHE = {}

def slugify(text: str) -> str:
    """Converts a string to a slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "-", text)
    text = re.sub(r"-+", "-", text)
    return text


class YetiIndicators(interface.BaseAnalyzer):
    """Analyzer for Yeti threat intel indicators."""

    NAME = "yetiindicators"
    DISPLAY_NAME = "Yeti CTI indicators"
    DESCRIPTION = "Mark events using CTI indicators from Yeti"

    DEPENDENCIES = frozenset(["domain"])

    def __init__(self, index_name, sketch_id, timeline_id=None):
        """Initialize the Analyzer.

        Args:
            index_name: OpenSearch index name
            sketch_id: The ID of the sketch.
            timeline_id: The ID of the timeline.
        """
        super().__init__(index_name, sketch_id, timeline_id=timeline_id)
        self.intel = {}
        root = current_app.config.get("YETI_API_ROOT")
        if root.endswith("/"):
            root = root[:-1]
        self.yeti_api_root = root
        self.yeti_web_root = root.replace("/api/v2", "")
        self.yeti_api_key = current_app.config.get("YETI_API_KEY")
        self._yeti_session = requests.Session()
        tls_cert = current_app.config.get("YETI_TLS_CERTIFICATE")
        if tls_cert and self.yeti_web_root.startswith("https://"):
            self._yeti_session.verify = tls_cert

    @property
    def authenticated_session(self) -> requests.Session:
        """Returns a requests.Session object with an authenticated Yeti session.

        Returns:
          A requests.Session object with an authenticated Yeti session.
        """
        if not self._yeti_session.headers.get("authorization"):
            self.authenticate_session()
        return self._yeti_session

    def authenticate_session(self) -> None:
        """Fetches an access token for Yeti."""

        response = self._yeti_session.post(
            f"{self.yeti_api_root}/auth/api-token",
            headers={"x-yeti-apikey": self.yeti_api_key},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Error {response.status_code} authenticating with Yeti API"
            )

        access_token = response.json()["access_token"]
        self._yeti_session.headers.update({"authorization": f"Bearer {access_token}"})

    def get_neighbors(self, yeti_object: Dict) -> List[Dict]:
        """Retrieves a list of neighbors associated to a given entity.

        Args:
          yeti_object: The Yeti object to get neighbors from.

        Returns:
          A list of JSON objects describing a Yeti entity.
        """
        if yeti_object["id"] in NEIGHBOR_CACHE:
            return NEIGHBOR_CACHE[yeti_object["id"]]

        extended_id = f"{yeti_object['root_type']}/{yeti_object['id']}"
        results = self.authenticated_session.post(
            f"{self.yeti_api_root}/graph/search",
            json={
                "source": extended_id,
                "graph": "links",
                "hops": 1,
                "direction": "any",
                "include_original": False,
            },
        )
        if results.status_code != 200:
            return []
        neighbors = []
        for neighbor in results.json().get("vertices", {}).values():
            if neighbor["root_type"] == "entity":
                neighbors.append(neighbor)
        NEIGHBOR_CACHE[yeti_object["id"]] = neighbors
        return neighbors

    def get_indicators(self, indicator_type: str) -> Dict[str, dict]:
        """Populates the intel attribute with entities from Yeti.

        Returns:
            A dictionary of indicators obtained from yeti, keyed by indicator
            ID.
        """
        response = self.authenticated_session.post(
            self.yeti_api_root + "/indicators/search",
            json={"query": {"name": ""}, "type": indicator_type, "count": 0},
        )
        data = response.json()
        if response.status_code != 200:
            raise RuntimeError(
                f"Error {response.status_code} retrieving indicators from Yeti:"
                + str(data)
            )
        indicators = {item["id"]: item for item in data["indicators"]}
        for _id, indicator in indicators.items():
            indicator["compiled_regexp"] = re.compile(indicator["pattern"])
            indicators[_id] = indicator
        return indicators

    def mark_event(
        self, indicator: Dict, event: interface.Event, neighbors: List[Dict]
    ):
        """Annotate an event with data from indicators and neighbors.

        Tags with skull emoji, adds a comment to the event.
        Args:
            indicator: a dictionary representing a Yeti indicator object.
            event: a Timesketch sketch Event object.
            neighbors: a list of Yeti entities related to the indicator.
        """
        tags = [slugify(tag) for tag in indicator["relevant_tags"]]
        is_intel = False
        for n in neighbors:
            tags.append(slugify(n["name"]))
            emoji_name = TYPE_TO_EMOJI[n["type"]]
            event.add_emojis([emojis.get_emoji(emoji_name)])

        event.add_tags(tags)
        event.commit()

        msg = f'Indicator match: "{indicator["name"]}" (ID: {indicator["id"]})\n'
        if neighbors:
            msg += f'Related entities: {[n["name"] for n in neighbors]}'
            is_intel = True

        comments = {c.comment for c in event.get_comments()}
        if msg not in comments:
            event.add_comment(msg)
            event.commit()

        return is_intel

    def run(self):
        """Entry point for the analyzer.

        Returns:
            String with summary of the analyzer result
        """
        if not self.yeti_api_root or not self.yeti_api_key:
            return "No Yeti configuration settings found, aborting."

        indicators = self.get_indicators("regex")

        entities_found = set()
        total_matches = 0
        new_indicators = set()

        intelligence_attribute = {"data": []}
        existing_refs = set()

        try:
            intelligence_attribute = self.sketch.get_sketch_attributes("intelligence")
            existing_refs = {
                (ioc["ioc"], ioc["externalURI"])
                for ioc in intelligence_attribute["data"]
            }
        except ValueError:
            print("Intelligence not set on sketch, will create from scratch.")

        intelligence_items = []
        indicators_processed = 0
        for indicator in indicators.values():
            query_dsl = {
                "query": {
                    "regexp": {"message.keyword": ".*" + indicator["pattern"] + ".*"}
                }
            }
            events = self.event_stream(
                query_dsl=query_dsl,
                return_fields=["message"],
                scroll=False)

            uri = f"{self.yeti_web_root}/indicators/{indicator['id']}"

            neighbors = None
            for event in events:
                total_matches += 1

                neighbors = self.get_neighbors(indicator)
                is_intel = self.mark_event(indicator, event, neighbors)

                regex = indicator["compiled_regexp"]
                match = regex.search(event.source.get("message"))
                if match:
                    match_in_sketch = match.group()
                else:
                    match_in_sketch = indicator["pattern"]

                intel = {
                    "externalURI": uri,
                    "ioc": match_in_sketch,
                    "tags": [n["name"] for n in neighbors],
                    "type": "other",
                }
                if is_intel and (match_in_sketch, uri) not in existing_refs:
                    intelligence_items.append(intel)
                    existing_refs.add((match_in_sketch, uri))
                new_indicators.add(indicator["id"])

            if neighbors:
                for n in [n for n in neighbors if n["root_type"] == "entity"]:
                    entities_found.add(f"{n['name']}:{n['type']}")

            indicators_processed += 1

        if not total_matches:
            self.output.result_status = "SUCCESS"
            self.output.result_priority = "NOTE"
            note = "No indicators were found in the timeline."
            self.output.result_summary = note
            return str(self.output)

        for entity in entities_found:
            name, _type = entity.split(":")
            self.sketch.add_view(
                f"Indicator matches for {name} ({_type})",
                self.NAME,
                query_string=f'tag:"{name}"',
            )

        all_iocs = intelligence_attribute["data"]
        all_iocs.extend(intelligence_items)
        self.sketch.add_sketch_attribute(
            "intelligence",
            [json.dumps({"data": all_iocs})],
            ontology="intelligence",
            overwrite=True,
        )

        success_note = (
            f"{total_matches} events matched {len(new_indicators)} "
            f"new indicators (out of {indicators_processed} processed)."
        )
        self.output.result_priority = "NOTE"
        if entities_found:
            success_note += f"\nEntities found: {', '.join(entities_found)}"
            self.output.result_priority = "HIGH"
        self.output.result_status = "SUCCESS"
        self.output.result_summary = success_note

        return str(self.output)


manager.AnalysisManager.register_analyzer(YetiIndicators)
