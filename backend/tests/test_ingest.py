"""Finding files in a bucket: naming, the scan, and the S3 client over real HTTP."""

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto.server import ThreadedMotoServer
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from recon.config import Settings
from recon.datagen.generate import Config, generate_dataset
from recon.datagen.mappings import INVOICE_MAPPING, PAYMENT_MAPPING
from recon.datagen.writer import write_dataset
from recon.ingest.naming import BadKeyError, parse_key, slugify
from recon.ingest.scan import scan_bucket
from recon.ingest.store import ObjectInfo, ObjectTooLargeError, S3Store
from recon.jobs.handlers import handlers_for
from recon.jobs.kinds import RECONCILE_PERIOD, SCAN_BUCKET
from recon.jobs.worker import Periodic, Worker
from recon.persistence.models import IngestedObject, Job, SourceFileVersion
from recon.persistence.repository import create_profile
from recon.profiles import ProfileConfig

NAME = "Spanish ERP and bank"
SLUG = "spanish-erp-and-bank"
BIG = 50 * 1024 * 1024


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Spanish ERP and bank", "spanish-erp-and-bank"),
        ("  Demo: invoices & payments! ", "demo-invoices-payments"),
        ("ACME_2026", "acme-2026"),
    ],
)
def test_a_profile_name_becomes_the_first_part_of_the_key(name: str, slug: str) -> None:
    assert slugify(name) == slug


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("acme/2026-01/invoices.csv", ("acme", "2026-01", "invoices")),
        ("acme/2026-12/payments-2026-12-31.csv", ("acme", "2026-12", "payments")),
        ("acme/2026-01/payments_final.csv", ("acme", "2026-01", "payments")),
        ("incoming/acme/2026-01/invoices.csv", ("acme", "2026-01", "invoices")),
    ],
)
def test_a_key_that_follows_the_convention_is_placed(
    key: str, expected: tuple[str, str, str]
) -> None:
    parsed = parse_key(key, "incoming/" if key.startswith("incoming/") else "")

    assert (parsed.profile, parsed.period, parsed.source) == expected


@pytest.mark.parametrize(
    "key",
    [
        "invoices.csv",
        "acme/invoices.csv",
        "acme/2026-13/invoices.csv",
        "acme/2026-1/invoices.csv",
        "Acme/2026-01/invoices.csv",
        "acme/2026-01/invoices.xlsx",
        "acme/2026-01/sub/invoices.csv",
        "acme/2026-01/invoices2.csv",
    ],
)
def test_a_key_that_does_not_follow_it_says_so(key: str) -> None:
    with pytest.raises(BadKeyError, match="does not look like"):
        parse_key(key)


def test_a_key_outside_the_prefix_is_refused() -> None:
    with pytest.raises(BadKeyError, match="prefix"):
        parse_key("other/acme/2026-01/invoices.csv", "incoming/")


@dataclass
class MemoryStore:
    bucket: str = "bucket"
    objects: dict[str, bytes] = field(default_factory=dict)
    etags: dict[str, str] = field(default_factory=dict)
    broken_on: str | None = None

    def put(self, key: str, data: bytes, etag: str | None = None) -> None:
        self.objects[key] = data
        self.etags[key] = etag or hashlib.md5(data).hexdigest()

    def list(self, prefix: str) -> Iterator[ObjectInfo]:
        for key in sorted(self.objects):
            if key.startswith(prefix):
                data = self.objects[key]
                yield ObjectInfo(key, self.etags[key], len(data))

    def get(self, key: str, max_bytes: int) -> bytes:
        if key == self.broken_on:
            raise ConnectionError("the store went away")
        if len(self.objects[key]) > max_bytes:
            raise ObjectTooLargeError(key)
        return self.objects[key]


@dataclass
class World:
    url: str
    root: Path
    sessions: sessionmaker[Session]
    invoices: bytes
    payments: bytes
    store: MemoryStore

    def make_profile(self, name: str = NAME) -> uuid.UUID:
        with self.sessions.begin() as session:
            return create_profile(
                session,
                name=name,
                reconciliation_type="invoice_payment",
                config=ProfileConfig(
                    sources={"invoices": INVOICE_MAPPING, "payments": PAYMENT_MAPPING}
                ),
            ).id

    def scan(self, *, prefix: str = "", max_bytes: int = BIG, store: MemoryStore | None = None):  # type: ignore[no-untyped-def]
        return scan_bucket(
            self.sessions,
            store or self.store,
            prefix=prefix,
            storage_dir=self.root / "storage",
            max_bytes=max_bytes,
        )

    def count(self, model: type, *where: ColumnElement[bool]) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model).where(*where)) or 0

    def jobs(self, kind: str) -> int:
        return self.count(Job, Job.kind == kind)


@pytest.fixture
def world(migrated_database_url: str, tmp_path: Path) -> Iterator[World]:
    data = tmp_path / "data"
    write_dataset(generate_dataset(Config(seed=4, invoices=100)), data)
    engine = create_engine(migrated_database_url)
    yield World(
        migrated_database_url,
        tmp_path,
        sessionmaker(engine),
        (data / "invoices.csv").read_bytes(),
        (data / "payments.csv").read_bytes(),
        MemoryStore(),
    )
    engine.dispose()


def test_files_named_by_the_convention_are_registered_and_a_complete_period_is_queued(
    world: World,
) -> None:
    world.make_profile()
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)
    world.store.put(f"{SLUG}/2026-01/payments-2026-01-31.csv", world.payments)

    result = world.scan()

    assert (result.seen, result.registered, result.skipped, result.failed) == (2, 2, 0, 0)
    assert result.reconciliations_queued == 1
    assert world.count(SourceFileVersion) == 2
    assert world.jobs(RECONCILE_PERIOD) == 1
    assert world.count(IngestedObject, IngestedObject.status == "registered") == 2


def test_a_period_with_only_one_file_is_not_queued(world: World) -> None:
    world.make_profile()
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)

    result = world.scan()

    assert result.registered == 1 and result.reconciliations_queued == 0
    assert world.jobs(RECONCILE_PERIOD) == 0


def test_a_second_scan_reads_nothing_again(world: World) -> None:
    world.make_profile()
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)
    world.store.put(f"{SLUG}/2026-01/payments.csv", world.payments)
    world.scan()
    reads = 0
    original = world.store.get

    def counting(key: str, max_bytes: int) -> bytes:
        nonlocal reads
        reads += 1
        return original(key, max_bytes)

    world.store.get = counting  # type: ignore[method-assign]

    again = world.scan()

    assert (again.seen, again.already_done, again.registered) == (2, 2, 0)
    assert reads == 0 and world.count(SourceFileVersion) == 2 and world.jobs(RECONCILE_PERIOD) == 1


def test_an_overwritten_object_with_new_content_is_a_new_version(world: World) -> None:
    world.make_profile()
    key = f"{SLUG}/2026-01/payments.csv"
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)
    world.store.put(key, world.payments)
    world.scan()
    corrected = world.payments.replace(b"PAY-000001", b"PAY-900001", 1)
    assert corrected != world.payments
    world.store.put(key, corrected)

    result = world.scan()

    assert (result.registered, result.already_done) == (1, 1)
    with world.sessions() as session:
        numbers = sorted(
            session.scalars(
                select(SourceFileVersion.version_number).where(
                    SourceFileVersion.source_name == "payments"
                )
            )
        )
    assert numbers == [1, 2]


def test_the_same_bytes_under_a_new_etag_are_noted_and_change_nothing(world: World) -> None:
    world.make_profile()
    key = f"{SLUG}/2026-01/payments.csv"
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)
    world.store.put(key, world.payments, etag="first")
    world.scan()
    # the same file uploaded again, as a multipart upload gets another ETag for the same bytes
    world.store.put(key, world.payments, etag="second")

    result = world.scan()

    assert (result.unchanged, result.registered, result.reconciliations_queued) == (1, 0, 0)
    assert world.count(SourceFileVersion) == 2
    assert world.jobs(RECONCILE_PERIOD) == 1


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("notes.txt", "does not look like"),
        (f"{SLUG}/2026-01/refunds.csv", "has no source 'refunds'"),
        ("nobody/2026-01/invoices.csv", "no profile is named 'nobody'"),
    ],
)
def test_an_object_that_cannot_be_placed_says_why_and_is_not_lost(
    world: World, key: str, reason: str
) -> None:
    world.make_profile()
    world.store.put(key, world.invoices)

    result = world.scan()

    assert (result.skipped, result.registered) == (1, 0)
    assert reason in result.reasons[0]
    assert world.count(SourceFileVersion) == 0
    with world.sessions() as session:
        row = session.scalars(select(IngestedObject)).one()
    assert row.status == "skipped" and reason in (row.reason or "")


def test_an_object_waiting_for_its_profile_is_registered_once_the_profile_exists(
    world: World,
) -> None:
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)
    assert world.scan().skipped == 1

    world.make_profile()
    result = world.scan()

    assert (result.registered, result.skipped) == (1, 0)
    assert world.count(IngestedObject) == 1


def test_two_profiles_with_the_same_slug_are_not_guessed_between(world: World) -> None:
    world.make_profile("Acme books")
    world.make_profile("acme-books")
    world.store.put("acme-books/2026-01/invoices.csv", world.invoices)

    result = world.scan()

    assert result.skipped == 1 and "share the name" in result.reasons[0]


def test_an_object_over_the_limit_is_skipped_without_being_read(world: World) -> None:
    world.make_profile()
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)

    result = world.scan(max_bytes=100)

    assert result.skipped == 1 and "limit" in result.reasons[0]


def test_a_file_that_cannot_be_read_fails_once_and_is_not_retried(world: World) -> None:
    world.make_profile()
    world.store.put(f"{SLUG}/2026-01/invoices.csv", b"just,some\nunrelated,columns\n")

    first = world.scan()
    second = world.scan()

    assert first.failed == 1 and "columns not found" in first.reasons[0]
    assert (second.failed, second.already_done) == (0, 1)
    assert world.count(SourceFileVersion) == 0
    assert not any((world.root / "storage" / "ingest").glob("*.csv"))


def test_only_the_prefix_is_scanned(world: World) -> None:
    world.make_profile()
    world.store.put(f"incoming/{SLUG}/2026-01/invoices.csv", world.invoices)
    world.store.put(f"elsewhere/{SLUG}/2026-01/payments.csv", world.payments)

    result = world.scan(prefix="incoming/")

    assert (result.seen, result.registered) == (1, 1)


def test_a_store_that_fails_mid_scan_keeps_what_was_done_and_raises(world: World) -> None:
    world.make_profile()
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)
    world.store.put(f"{SLUG}/2026-01/payments.csv", world.payments)
    world.store.broken_on = f"{SLUG}/2026-01/payments.csv"

    with pytest.raises(ConnectionError):
        world.scan()
    world.store.broken_on = None
    result = world.scan()

    assert (result.already_done, result.registered) == (1, 1)
    assert world.jobs(RECONCILE_PERIOD) == 1


def test_a_worker_scans_on_a_timer_and_then_reconciles(world: World) -> None:
    world.make_profile()
    world.store.put(f"{SLUG}/2026-01/invoices.csv", world.invoices)
    world.store.put(f"{SLUG}/2026-01/payments.csv", world.payments)
    settings = Settings(
        database_url=world.url, storage_dir=world.root / "storage", s3_bucket="bucket"
    )
    # a little ahead of the real time that the scan stamps on the jobs it queues
    clock = [datetime.now(UTC) + timedelta(seconds=5)]
    worker = Worker(
        world.sessions,
        handlers_for(world.sessions, world.store, settings),
        name="w",
        backoff=lambda n: 0.0,
        clock=lambda: clock[0],
        periodic=[Periodic(SCAN_BUCKET, "bucket", {"bucket": "bucket"}, timedelta(seconds=30))],
    )

    assert worker.run_once() == "succeeded"  # the scan
    assert worker.run_once() == "succeeded"  # the reconciliation it queued
    assert worker.run_once() == "succeeded"  # the alerts that reconciliation queued
    assert worker.run_once() is None
    assert world.jobs(SCAN_BUCKET) == 1

    clock[0] += timedelta(seconds=10)
    assert worker.run_once() is None  # not yet time to look again
    clock[0] += timedelta(seconds=30)
    assert worker.run_once() == "succeeded"
    assert world.jobs(SCAN_BUCKET) == 2
    with world.sessions() as session:
        done = session.scalars(select(Job).where(Job.kind == RECONCILE_PERIOD)).one()
    assert done.status == "succeeded" and done.result is not None
    assert done.result["cases"]["applied"]


@pytest.fixture(scope="module")
def s3_endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def bucket(s3_endpoint: str) -> str:
    name = f"b{uuid.uuid4().hex[:12]}"
    client = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    client.create_bucket(Bucket=name)
    for key, body in (("a/one.csv", b"1"), ("a/two.csv", b"22"), ("b/three.csv", b"333")):
        client.put_object(Bucket=name, Key=key, Body=body)
    return name


def s3(endpoint: str, bucket: str) -> S3Store:
    return S3Store(bucket, endpoint_url=endpoint, access_key="test", secret_key="test")


def test_the_s3_store_lists_by_prefix_and_reads_over_http(s3_endpoint: str, bucket: str) -> None:
    store = s3(s3_endpoint, bucket)

    listed = list(store.list("a/"))

    assert [(o.key, o.size) for o in listed] == [("a/one.csv", 1), ("a/two.csv", 2)]
    assert all(o.etag and '"' not in o.etag for o in listed)
    assert store.get("a/two.csv", 100) == b"22"


def test_the_s3_store_refuses_an_object_over_the_limit(s3_endpoint: str, bucket: str) -> None:
    with pytest.raises(ObjectTooLargeError):
        s3(s3_endpoint, bucket).get("b/three.csv", 2)


def test_the_s3_store_lists_past_the_first_page(s3_endpoint: str, bucket: str) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    for n in range(1001):
        client.put_object(Bucket=bucket, Key=f"many/{n:04d}.csv", Body=b"x")

    keys = [o.key for o in s3(s3_endpoint, bucket).list("many/")]

    assert len(keys) == 1001 and keys == sorted(keys)


def test_a_missing_object_is_an_error_not_an_empty_file(s3_endpoint: str, bucket: str) -> None:
    with pytest.raises(Exception, match="NoSuchKey"):
        s3(s3_endpoint, bucket).get("nope.csv", 100)
