"""What an alert says. Plain facts from the cases, with the amounts at stake by currency."""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recon.domain.money import format_amount
from recon.persistence.models import ExceptionCase, Profile

OPEN = ("open", "fix_reported")
MAX_PERIOD_LINES = 10


@dataclass(frozen=True)
class Alert:
    subject: str
    lines: tuple[str, ...]
    link: str | None = None
    facts: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        body = "\n".join(self.lines)
        return f"{self.subject}\n\n{body}" + (f"\n\n{self.link}" if self.link else "")


def _link(public_url: str | None, profile_id: uuid.UUID, period: str) -> str | None:
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}/?profile={profile_id}&period={period}"


def at_stake(session: Session, profile_id: uuid.UUID, period: str | None = None) -> dict[str, int]:
    """The absolute difference of the open cases, in minor units, by currency."""
    statement = (
        select(ExceptionCase.currency, func.sum(func.abs(ExceptionCase.variance_minor)))
        .where(
            ExceptionCase.profile_id == profile_id,
            ExceptionCase.status.in_(OPEN),
            ExceptionCase.currency.is_not(None),
        )
        .group_by(ExceptionCase.currency)
        .order_by(ExceptionCase.currency)
    )
    if period is not None:
        statement = statement.where(ExceptionCase.period == period)
    return {currency: int(total or 0) for currency, total in session.execute(statement) if currency}


def _money(amounts: dict[str, int]) -> str:
    if not amounts:
        return "no amount known"
    return ", ".join(f"{format_amount(m, c, thousands=True)} {c}" for c, m in amounts.items())


def open_count(session: Session, profile_id: uuid.UUID, period: str | None = None) -> int:
    statement = select(func.count()).where(
        ExceptionCase.profile_id == profile_id, ExceptionCase.status.in_(OPEN)
    )
    if period is not None:
        statement = statement.where(ExceptionCase.period == period)
    return int(session.scalar(statement) or 0)


def run_alert(
    session: Session,
    profile_id: uuid.UUID,
    period: str,
    cases: dict[str, int],
    public_url: str | None,
) -> Alert:
    """After a run: what changed in the cases and what is open now."""
    profile = session.get_one(Profile, profile_id)
    opened, reopened = cases.get("opened", 0), cases.get("reopened", 0)
    new = opened + reopened
    return Alert(
        subject=f"{profile.name}: {new} new exception{'s' if new != 1 else ''} in {period}",
        lines=(
            f"This run opened {opened}, reopened {reopened} "
            f"and resolved {cases.get('resolved', 0)}.",
            f"Open now in {period}: {open_count(session, profile_id, period)}, "
            f"{_money(at_stake(session, profile_id, period))} at stake.",
        ),
        link=_link(public_url, profile_id, period),
        facts={"profile": profile.name, "period": period, "new": str(new)},
    )


def digest(session: Session, public_url: str | None) -> Alert | None:
    """What is open across every profile and period, or None when nothing is."""
    rows = session.execute(
        select(ExceptionCase.profile_id, ExceptionCase.period, func.count())
        .where(ExceptionCase.status.in_(OPEN))
        .group_by(ExceptionCase.profile_id, ExceptionCase.period)
        .order_by(func.count().desc(), ExceptionCase.period.desc())
    ).all()
    if not rows:
        return None
    total = sum(count for _, _, count in rows)
    lines = []
    for profile_id, period, count in rows[:MAX_PERIOD_LINES]:
        profile = session.get_one(Profile, profile_id)
        lines.append(
            f"{profile.name}, {period}: {count} open, "
            f"{_money(at_stake(session, profile_id, period))} at stake"
        )
    if len(rows) > MAX_PERIOD_LINES:
        lines.append(f"…and {len(rows) - MAX_PERIOD_LINES} more periods")
    first = rows[0]
    return Alert(
        subject=f"Reconciliation digest: {total} open exception{'s' if total != 1 else ''}",
        lines=tuple(lines),
        link=_link(public_url, first[0], first[1]),
    )
