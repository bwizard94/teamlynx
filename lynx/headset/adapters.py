"""Adapters between relay state (:mod:`lynx.net`) and the vision / HUD types.

Field mapping (no unit or angle conversion is needed; both sides use ENU metres and the
compass-heading / nose-up pitch / right-side-down roll convention of ``docs/spatial-math.md``):

=====================  =========================================================
Vision / HUD type      Relay source
=====================  =========================================================
``OperatorPose``       local :class:`lynx.spatial.Pose` + node id, callsign, team
``TeammateTrack``      :class:`lynx.net.state.NodeState` (``Telemetry`` + ``last_seen``)
``WorldPing``          :class:`lynx.net.state.PingState` (``Ping`` keyed by ``(owner, ping_id)``)
=====================  =========================================================

``TeammateTrack.timestamp`` is the receiver-side ``time.monotonic()`` of the last accepted
telemetry, so the IFF associator must be updated with ``now=time.monotonic()``.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from lynx.hud.types import WorldPing
from lynx.net.schema import PingType, Team, Telemetry
from lynx.net.state import NodeState, PingKey, PingState
from lynx.spatial import Pose
from lynx.vision.types import OperatorPose, TeammateTrack

# The HUD palette only has friendly colours (green/blue/...); unknown names such as "red" render in
# the default friendly green so a teammate can never be drawn in the TANGO colour.
TEAM_COLOR_NAME: Dict[Team, str] = {
    Team.BLUE: "blue",
    Team.GREEN: "green",
    Team.RED: "red",
    Team.AMBER: "amber",
}

PING_COLOR_BGR: Dict[PingType, Optional[Tuple[int, int, int]]] = {
    PingType.MARK: None,  # HUD default ping colour
    PingType.CONTACT: (0, 140, 255),
    PingType.MOVE: (255, 255, 0),
    PingType.DANGER: (60, 60, 255),
    PingType.RALLY: (120, 255, 140),
}


def team_color_name(team: Team | int) -> str:
    return TEAM_COLOR_NAME.get(Team(team), "green")


def world_ping_id(key: PingKey) -> int:
    """Collapse the relay's ``(owner u16, ping_id u32)`` key into one integer id."""
    owner, ping_id = key
    return (int(owner) << 32) | int(ping_id)


def operator_pose(pose: Pose, node_id: int, callsign: str, team: Team | int) -> OperatorPose:
    return OperatorPose.from_pose(pose, node_id, callsign, team_color_name(team))


def teammate_from_telemetry(tel: Telemetry, timestamp: float) -> TeammateTrack:
    return TeammateTrack(
        node_id=tel.node_id,
        callsign=tel.callsign,
        x=tel.x,
        y=tel.y,
        z=tel.z,
        team_color=team_color_name(tel.team),
        timestamp=timestamp,
        yaw=tel.heading,
    )


def teammate_from_node(ns: NodeState) -> TeammateTrack:
    return teammate_from_telemetry(ns.telemetry, ns.last_seen)


def teammates_from_nodes(nodes: Mapping[int, NodeState], self_node: int) -> List[TeammateTrack]:
    """Friendly-team telemetry only; the local node is excluded."""
    return [teammate_from_node(ns) for nid, ns in sorted(nodes.items()) if nid != self_node]


def world_ping_from(ps: PingState, callsigns: Mapping[int, str]) -> WorldPing:
    """Label is the ping type (``MARK``, ``CONTACT`` ...); owner is the owner's callsign."""
    p = ps.ping
    owner = callsigns.get(p.owner, f"N{p.owner}")
    return WorldPing(
        ping_id=world_ping_id(p.key),
        x=p.x,
        y=p.y,
        z=p.z,
        label=PingType(p.ping_type).name,
        owner=owner,
        color=PING_COLOR_BGR.get(PingType(p.ping_type)),
    )


def world_pings_from_state(
    pings: Mapping[PingKey, PingState], nodes: Mapping[int, NodeState], self_node: int, self_callsign: str
) -> List[WorldPing]:
    callsigns = {nid: ns.telemetry.callsign for nid, ns in nodes.items()}
    callsigns[self_node] = self_callsign
    ordered = sorted(pings.values(), key=lambda ps: ps.received)
    return [world_ping_from(ps, callsigns) for ps in ordered]
