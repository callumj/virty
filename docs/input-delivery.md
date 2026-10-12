# Guest typing delivery and retry retention

The manager reserves guest input ownership before secret access and keeps it
until delivery ends. Status, operation lookup and cancellation remain available
while broker access waits. Other input, screenshot and power commands are
refused while ownership is reserved. Broker lock acquisition, pinned-owner
validation and subprocess pipe I/O run off the main actor under one 15-second
monotonic deadline. Lock acquisition polls without blocking; stdout is capped
at 16 KiB and stderr is drained and discarded. Cancellation and timeout stop
the subprocess and release the lock. No authentication UI is requested.
Automatic first-start provisioning uses the same asynchronous broker path.
Legacy Keychain reads run off the main actor, but their in-process Apple APIs
cannot be forcibly cancelled; cancellation prevents subsequent guest delivery.

After lookup, the manager rechecks cancellation, immutable VM identity, running
state and display generation before injecting any key. These checks repeat
throughout typing. A cancellation that races with successful lookup still
prevents delivery. No secret value or public typed text is stored in the ledger.

## Permanent request IDs

Typing request IDs are 1–128 ASCII letters, digits or hyphens. An accepted ID is
retained indefinitely. Retrying an accepted request never types it again,
including after a lost response, worker restart or snapshot restore. Reuse for
a different command or secret alias is refused. The ledger does not compare the
public text of a repeated `type-text` request; the caller must choose a new ID
for a new delivery. A cancelled/failed secret lookup consumes its accepted ID;
use a new ID only after checking guest state and deciding a new delivery is safe.

`operations/<SHA256-first-byte>/<SHA256-requestId>.json` is an owned private indexed
record. Acceptance durably writes `started` before lookup or key injection;
completion writes `typed`, and an interrupted request writes `uncertain` when
possible. Records contain command, status, opaque alias where applicable, ID
and a process epoch. A `started` record from another epoch is reported as
`uncertain`, without rewriting the complete history. Uncertain operations are
never repeated automatically, even if no key was actually delivered.

Each acceptance/completion writes only its record with file sync, atomic rename
and directory sync. Lookup reads only the requested record. Startup reads the
small migration marker, verifies retained legacy file metadata, and reads no historical index. Storage grows linearly with
accepted IDs and is intentionally permanent; there is no pruning or retry
expiry. Browser keystrokes therefore still create entries, but do not cause
quadratic serialization, write volume or an in-memory history dictionary.

## Legacy migration and recovery

The first new-worker startup reads the former `operations.json` once and writes
all existing IDs to the indexed ledger. Legacy `started` entries become
`uncertain`. A private durable `operations/migration.json` marker is published
only after every migrated record is durable. Until that marker commits, restart
repeats migration before accepting requests and verifies existing partial
records. Conflicting or malformed records/markers fail closed. The original
legacy JSON contents are retained; its permissions are tightened to 0600 if
necessary. Interrupted atomic writes may leave private `.pending-*` files;
these do not represent accepted records and are never consulted for delivery.

Preserve both the indexed ledger and legacy migration source during VM
maintenance. Snapshots deliberately exclude input ledgers. Do not downgrade a
VM to a controller that understands only `operations.json` after indexed
operations have been accepted: the retained legacy file does not contain new
IDs. Re-upgrade detects changed legacy inode/size/modification/change metadata
and refuses to start, preserving both ledgers for recovery. This check does not
read the historical source on each startup. Deleting records or replacing the index with an older copy invalidates the
permanent duplicate-delivery guarantee. Corrupt records must be investigated
rather than removed to make a retry succeed.

## Disposable regression checks

`Tests/BrokerClientTests.swift` compiles the real broker client with a fake
executable and private fixture directory. It verifies a main-actor heartbeat
during lock contention, cancellation, whole-operation lock/child deadlines,
stdout overflow refusal and stderr draining. `Tests/OperationLedgerTests.swift`
compiles the real ledger and shared request-ID table. It covers permanent IDs,
lost responses, epoch uncertainty, approximately linear record storage,
repeatable partial migration, retained legacy contents and corrupt-record
refusal. These tests use public fixture values and no Keychain or live guest.
Live disposable-guest verification and production deployment remain separate.
