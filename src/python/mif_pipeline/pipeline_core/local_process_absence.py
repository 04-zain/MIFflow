"""Fresh administrative observations for the explicit trusted-local Linux route.

This is not a process completion or a drain receipt. It makes no claim about the
old exit status or processes that left the trusted local launch session.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import time
import uuid

from .durable_records import canonical_json_bytes
from .process_containment import LinuxLocalProcessGroupAttachmentV1


def _parse_stat(text):
    try:
        pid = int(text[:text.index(' (')])
        values = text[text.rindex(') ') + 2:].split()
        return (pid, int(values[1]), int(values[2]), int(values[3]), int(values[19]))
    except (ValueError, IndexError) as error:
        raise ValueError('A process identity could not be read; recovery is unavailable.') from error


def _require_absent(leader, rows):
    if any(pid == leader or group == leader or session == leader
           for pid, _parent, group, session, _start in rows):
        raise ValueError('The original process ID, group or session is live or reused; recovery is unavailable.')


def _scan(leader):
    rows = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            row = _parse_stat((entry / 'stat').read_text())
        except FileNotFoundError:
            # A process that exited during the scan cannot mutate the project.
            continue
        except OSError as error:
            raise ValueError('The process inventory is unreadable; recovery is unavailable.') from error
        if row[0] != int(entry.name):
            raise ValueError('The process inventory changed identity during inspection.')
        rows.append(row)
    _require_absent(leader, rows)
    # Reject an ID created or reused while the inventory was being traversed.
    if Path(f'/proc/{leader}').exists():
        raise ValueError('The original process ID reappeared during inspection.')
    return sorted(rows)


def observe_local_process_absence(*, launch):
    """Observe the selected launch twice, failing closed without sending signals."""
    from mif_pipeline.qupath_runtime.project_stage import ProjectStageProcessLaunchRecord

    if (not sys.platform.startswith('linux') or os.name != 'posix'
            or type(launch) is not ProjectStageProcessLaunchRecord
            or type(launch.launch_evidence) is not LinuxLocalProcessGroupAttachmentV1
            or launch.process_start_token.provider != 'linux-proc-start-ticks/v1'
            or launch.process_start_token.process_id != launch.launch_evidence.leader_process_id):
        raise ValueError('Administrative absence recovery requires the exact trusted-local Linux launch.')
    boot = str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
    machine = Path('/etc/machine-id').read_text().strip()
    if len(machine) != 32 or any(c not in '0123456789abcdef' for c in machine):
        raise ValueError('The local host identity is unavailable.')
    leader = launch.launch_evidence.leader_process_id
    first = _scan(leader)
    second = _scan(leader)
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot:
        raise ValueError('The host boot changed during process inspection.')
    value = {
        'schema_version': 'trusted-local-observed-absence/v1',
        'host_identity_sha256': hashlib.sha256(machine.encode()).hexdigest(),
        'boot_id': boot,
        'observed_monotonic_ns': time.monotonic_ns(),
        'process_launch_sha256': launch.process_launch_sha256,
        'launch_evidence': launch.launch_evidence.to_mapping(),
        'process_start_token': launch.process_start_token.to_mapping(),
        'scan_inventory_sha256s': [hashlib.sha256(canonical_json_bytes(rows)).hexdigest()
                                   for rows in (first, second)],
        'original_exit_status': None,
        'original_process_completion_known': False,
        'original_drain_reconstructed': False,
        'observed_group_and_session_absent': True,
    }
    return {**value, 'observation_sha256': hashlib.sha256(canonical_json_bytes(value)).hexdigest()}
