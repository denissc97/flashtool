"""Authentication 0x29 (APCE) and several ECUs on one bus."""

import threading

import pytest

from flashtool.app.job import run_group
from flashtool.app.manager import JobManager
from flashtool.firmware.loaders import FirmwareSpec, load_firmware
from flashtool.security.certs import (
    CertificateError, CertConfig, FileCertificateProvider, PythonCertificateProvider, TargetCredentials,
    create_certificate_provider,
)
from flashtool.security.seedkey import PythonSeedKey
from flashtool.sequence.engine import Engine
from flashtool.sequence.events import JobFinished, StepFinished
from flashtool.sequence.group import GroupError, GroupSync
from flashtool.sequence.loader import load_sequence
from flashtool.sim.can_server import CanEcuServer
from flashtool.sim.ecu import SimConfig, SimulatedEcu
from flashtool.sim.pki import make_test_pki
from flashtool.transport.isotp_can import CanTpConfig, IsoTpCanTransport
from flashtool.uds.client import UdsClient

from .conftest import SEEDKEY_PLUGIN

pytestmark = [pytest.mark.req("REQ-06"), pytest.mark.req("REQ-07"), pytest.mark.req("REQ-12")]

AUTH_TEMPLATE = """extends: uds_standard
patch:
  - replace: security
    with: {id: authenticate, authenticate: {challenge_client_length: 16}}
"""


@pytest.fixture
def pki(tmp_path):
    return make_test_pki(str(tmp_path / "pki"))


def _flash(tmp_path, ecu, credentials, template_text, channel):
    (tmp_path / "seq.yaml").write_text(template_text)
    image = load_firmware([FirmwareSpec(file="app.hex")], str(tmp_path))
    with CanEcuServer(ecu, channel=channel):
        cfg = CanTpConfig()
        cfg.hardware.channel = channel
        transport = IsoTpCanTransport(cfg)
        transport.open()
        try:
            seq = load_sequence("seq.yaml", str(tmp_path))
            engine = Engine(job="t", client=UdsClient(transport), transport=transport, image=image,
                            variables=seq["variables"], seedkey=PythonSeedKey(SEEDKEY_PLUGIN),
                            emit=lambda e: None, credentials=credentials)
            ok, err = engine.run(seq)
        finally:
            transport.close()
    return ok, err, engine, image


def test_authenticated_flash(project_dir, pki):
    ecu = SimulatedEcu(SimConfig(auth_trust_anchor=pki["root_ca.pem"], require_authentication=True))
    creds = TargetCredentials(FileCertificateProvider(pki["tester.pem"], pki["tester.key"]), "ecu", "dev")
    ok, err, engine, image = _flash(project_dir.dir, ecu, creds, AUTH_TEMPLATE, "a-ok")
    assert ok, err
    rec = next(r for r in engine.records if r.path == "authenticate")
    assert "FlashTool tester" in rec.detail["certificate"]["subject"]
    seg = image.blocks[0].segments[0]
    assert ecu.read_memory(seg.address, len(seg.data)) == seg.data


def test_certificate_from_untrusted_ca_rejected(project_dir, pki, tmp_path):
    other = make_test_pki(str(tmp_path / "other"))
    ecu = SimulatedEcu(SimConfig(auth_trust_anchor=pki["root_ca.pem"], require_authentication=True))
    creds = TargetCredentials(FileCertificateProvider(other["tester.pem"], other["tester.key"]), "ecu", "")
    ok, err, _, _ = _flash(project_dir.dir, ecu, creds, AUTH_TEMPLATE, "a-ca")
    assert not ok and "0x52" in err and not ecu.written


def test_wrong_key_for_certificate_rejected(project_dir, pki, tmp_path):
    other = make_test_pki(str(tmp_path / "other"))
    ecu = SimulatedEcu(SimConfig(auth_trust_anchor=pki["root_ca.pem"], require_authentication=True))
    creds = TargetCredentials(FileCertificateProvider(pki["tester.pem"], other["tester.key"]), "ecu", "")
    ok, err, _, _ = _flash(project_dir.dir, ecu, creds, AUTH_TEMPLATE, "a-pop")
    assert not ok and "0x58" in err


def test_security_access_alone_is_not_enough_when_authentication_required(project_dir, pki):
    ecu = SimulatedEcu(SimConfig(auth_trust_anchor=pki["root_ca.pem"], require_authentication=True))
    ok, err, _, _ = _flash(project_dir.dir, ecu, None, "extends: uds_standard\n", "a-27")
    assert not ok and "0x34" in err  # authenticationRequired


def test_python_certificate_plugin(tmp_path, pki):
    plugin = tmp_path / "adapter.py"
    plugin.write_text(f"""
from flashtool.security.certs import FileCertificateProvider
_p = FileCertificateProvider({pki['tester.pem']!r}, {pki['tester.key']!r})
calls = []
def certificate_chain(ecu, role):
    calls.append((ecu, role))
    return _p.certificate_chain(ecu, role)
def sign(ecu, role, data):
    return _p.sign(ecu, role, data)
""")
    provider = create_certificate_provider(CertConfig(provider="python", path=str(plugin)))
    assert isinstance(provider, PythonCertificateProvider)
    assert provider.certificate_chain("bcm", "dev")[0][:1] == b"\x30"  # DER sequence
    with pytest.raises(CertificateError, match="Q-10"):
        create_certificate_provider(CertConfig(provider="zenzefi"))


# -- bus groups ------------------------------------------------------------
def test_group_sync_runs_once_and_releases_on_leave():
    group = GroupSync(["a", "b", "c"], timeout_s=5)
    calls, results = [], {}

    def member(name):
        results[name] = group.run_once("main", "step", name, lambda: calls.append(name) or "done")

    group.leave("main", "c")  # c failed earlier: nobody waits for it
    threads = [threading.Thread(target=member, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert sorted(r[0] for r in results.values()) == [False, True]


def test_group_failure_reaches_every_member():
    group = GroupSync(["a", "b"], timeout_s=5)
    errors = {}

    def member(name):
        try:
            group.run_once("main", "s", name, lambda: 1 / 0)
        except Exception as exc:
            errors[name] = type(exc)

    threads = [threading.Thread(target=member, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(errors.values(), key=str) == sorted([ZeroDivisionError, GroupError], key=str)


GROUP_TARGETS = """
- id: ecu1
  simulate: true
  can: {backend: virtual, channel: g-bus, tx_id: 0x7E0, rx_id: 0x7E8}
  firmware: [app.hex]
  seed_key: {provider: python, path: seedkey.py}
- id: ecu2
  simulate: true
  can: {backend: virtual, channel: g-bus, tx_id: 0x7E1, rx_id: 0x7E9}
  firmware: [app.hex]
  seed_key: {provider: python, path: seedkey.py}
- id: ecu3
  simulate: true
  can: {backend: virtual, channel: g-bus, tx_id: 0x7E2, rx_id: 0x7EA}
  firmware: [app.hex]
  seed_key: {provider: python, path: seedkey.py}
  simulator: {secret: %s}
"""


def test_three_ecus_on_one_bus(project_dir):
    path = project_dir(GROUP_TARGETS % "0x5A3C96E1")
    events = []
    results = run_group(path, ["ecu1", "ecu2", "ecu3"], events.append)
    assert results == {"ecu1": True, "ecu2": True, "ecu3": True}
    for step in ("ext_session", "comm_off", "finally/default_session"):
        statuses = sorted(e.status for e in events if isinstance(e, StepFinished) and e.path == step)
        assert statuses == ["ok", "shared", "shared"], step


def test_failing_ecu_does_not_block_the_bus_group(project_dir):
    path = project_dir(GROUP_TARGETS % "0x11111111")  # ecu3 rejects our key
    events = []
    results = run_group(path, ["ecu1", "ecu2", "ecu3"], events.append)
    assert results == {"ecu1": True, "ecu2": True, "ecu3": False}
    post = [e for e in events if isinstance(e, StepFinished) and e.path == "finally/comm_on"]
    assert len(post) == 3 and sum(e.status == "ok" for e in post) == 1


def test_processes_per_bus_and_credential_broker(project_dir, pki):
    path = project_dir(GROUP_TARGETS % "0x5A3C96E1" + f"""
- id: secure
  transport: doip
  simulate: true
  firmware: [app.hex]
  sequence: auth.yaml
  certificates: {{provider: file, cert: {pki['tester.pem']}, key: {pki['tester.key']}, role: supplier}}
  simulator: {{auth_trust_anchor: {pki['root_ca.pem']}, require_authentication: true}}
""")
    (project_dir.dir / "auth.yaml").write_text(AUTH_TEMPLATE)
    manager = JobManager()
    groups = manager.start(path, ["ecu1", "ecu2", "ecu3", "secure"])
    try:
        finished = {e.job: e for e in manager.wait_all() if isinstance(e, JobFinished)}
        usage = manager.credential_usage()
    finally:
        manager.shutdown()
    assert sorted(map(sorted, groups)) == [["ecu1", "ecu2", "ecu3"], ["secure"]]
    assert all(e.success for e in finished.values()), {k: v.error for k, v in finished.items()}
    assert [u["op"] for u in usage] == ["certificate_chain", "sign"]
