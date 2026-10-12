#!/usr/bin/env python3
"""Test self-signed cross-build Keychain access without changing the VM owner."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".build/signing-helper"


def run(arguments, **options):
    result = subprocess.run([str(a) for a in arguments], capture_output=True, timeout=60, **options)
    if result.returncode:
        detail = result.stderr.decode().strip()
        if Path(str(arguments[0])).name in ("first", "second", "outsider") and result.stdout:
            detail = json.loads(result.stdout).get("error", "Probe failed")
        raise RuntimeError(f"{Path(str(arguments[0])).name} failed (exit {result.returncode}): {detail}")
    return result.stdout


def compile_helper():
    HELPER.parent.mkdir(exist_ok=True)
    run(["xcrun", "clang", "-fobjc-arc", "-Wall", "-Wextra", "-Werror", "-framework", "Foundation",
         "-framework", "Security", ROOT / "scripts/signing-helper.m", "-o", HELPER])
    run(["codesign", "--force", "--sign", "-", "--identifier", "com.virty.signing-helper", HELPER])


def helper(action, **values):
    result = subprocess.run([str(HELPER)], input=json.dumps({"action": action, **values}).encode(),
                            capture_output=True, timeout=20)
    response = json.loads(result.stdout)
    if result.returncode or response["status"]:
        raise RuntimeError(f"Signing Keychain {action} failed (OSStatus {response['status']}); no UI requested.")


def wrap_arguments(password, command):
    # OpenSSL receives two independent password streams, never argv/environment.
    readers = []
    try:
        for _ in range(2):
            reader, writer = os.pipe()
            os.write(writer, password.encode() + b"\n")
            os.close(writer)
            readers.append(reader)
        return run([*command, "-passin", f"fd:{readers[0]}", "-passout", f"fd:{readers[1]}"],
                   pass_fds=readers)
    finally:
        for reader in readers:
            os.close(reader)


def make_identity(directory, name, keychain, keychain_password):
    password = secrets.token_urlsafe(48)
    config = directory / "certificate.cnf"
    config.write_text(f"""[req]
prompt=no
distinguished_name=subject
x509_extensions=extensions
[subject]
CN={name}
[extensions]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
""")
    certificate = directory / "certificate.pem"
    private = directory / "private.encrypted.pem"
    wrapped = directory / "identity.encrypted.p12"
    try:
        run(["openssl", "req", "-new", "-x509", "-newkey", "rsa:3072", "-sha256", "-days", "3650",
             "-config", config, "-keyout", private, "-out", certificate, "-passout", "stdin"],
            input=password.encode() + b"\n")
        wrap_arguments(password, ["openssl", "pkcs12", "-export", "-inkey", private, "-in", certificate,
                                  "-out", wrapped, "-name", name])
        helper("import", pkcs12=str(wrapped), passphrase=password,
               keychain=str(keychain), password=keychain_password)
        der = run(["openssl", "x509", "-in", certificate, "-outform", "DER"])
        return {"fingerprint": hashlib.sha1(der).hexdigest().upper(), "name": name,
                "certificate": str(certificate)}
    finally:
        private.unlink(missing_ok=True)
        wrapped.unlink(missing_ok=True)


def sign(executable, fingerprint, identifier, keychain=None):
    arguments = ["codesign", "--force", "--sign", fingerprint, "--timestamp=none",
                 "--identifier", identifier, "--requirements",
                 f'=designated => identifier "{identifier}" and certificate leaf = H"{fingerprint}"']
    if keychain:
        arguments.extend(["--keychain", keychain])
    run([*arguments, executable])
    run(["codesign", "--verify", "--strict", executable])


def signing_info(executable):
    result = subprocess.run(["codesign", "--display", "--verbose=4", "--requirements", "-", str(executable)],
                            capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Cannot inspect the disposable code signature.")
    output = result.stdout.decode() + result.stderr.decode()
    digest = re.search(r"^CDHash=(.+)$", output, re.MULTILINE)
    requirement = re.search(r"^designated => (.+)$", output, re.MULTILINE)
    if not digest or not requirement:
        raise RuntimeError("Disposable signature metadata was incomplete.")
    return {"cdhash": digest[1], "requirement": requirement[1]}


def read_probe(executable, item_id):
    result = subprocess.run([str(executable), "read", item_id], capture_output=True, timeout=20)
    value = json.loads(result.stdout)
    if result.returncode not in (0, 1) or value.get("ok") != (result.returncode == 0):
        raise RuntimeError("The disposable reader failed unexpectedly.")
    return value


def probe():
    compile_helper()
    original_search = run(["security", "list-keychains", "-d", "user"])
    with tempfile.TemporaryDirectory(prefix="virty-signing-", dir=ROOT / ".build") as temporary:
        folder = Path(temporary)
        keychain = folder / "test.keychain-db"
        password = secrets.token_urlsafe(48)
        helper("create", keychain=str(keychain), password=password)
        item_id = str(uuid.uuid4())
        created = False
        first = folder / "first"
        try:
            identity = make_identity(folder, "Virty Signing Test " + item_id, keychain, password)
            object_file = folder / "compat.o"
            run(["xcrun", "clang", "-fobjc-arc", "-c", ROOT / "Sources/KeychainCompat.m", "-o", object_file])
            diagnostics = folder / "diagnostics.o"
            run(["xcrun", "clang", "-fobjc-arc", "-Wall", "-Wextra", "-Werror", "-c",
                 ROOT / "Tests/SigningDiagnostics.m", "-o", diagnostics])
            second = folder / "second"
            for executable, flags in ((first, []), (second, ["-D", "SECOND_BUILD"])):
                run(["xcrun", "swiftc", "-swift-version", "5", "-warnings-as-errors",
                     "-module-cache-path", "/tmp/virty-swift-module-cache", "-import-objc-header",
                     ROOT / "Tests/SigningProbe.h", ROOT / "Sources/Support.swift",
                     ROOT / "Sources/SecretStore.swift", ROOT / "Sources/BrokerClient.swift", ROOT / "Tests/SigningProbe.swift",
                     object_file, diagnostics, *flags, "-o", executable])
                sign(executable, identity["fingerprint"], "com.virty.signing-probe", keychain)
            assert hashlib.sha256(first.read_bytes()).digest() != hashlib.sha256(second.read_bytes()).digest()
            first_signature, second_signature = signing_info(first), signing_info(second)
            assert first_signature["requirement"] == second_signature["requirement"]
            assert first_signature["cdhash"] != second_signature["cdhash"]
            run([first, "create", item_id])
            created = True
            run([first, "read", item_id])
            metadata = json.loads(run([first, "inspect", item_id]))
            partitions = []
            for value in metadata["partitions"]:
                if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", value):
                    partitions.extend(plistlib.loads(bytes.fromhex(value))["Partitions"])
                else:
                    partitions.append(value)
            changed = read_probe(second, item_id)
            original = folder / "original"
            shutil.copyfile(first, original)
            original.chmod(0o700)
            shutil.copyfile(second, first)
            try:
                same_path = read_probe(first, item_id)
            finally:
                shutil.copyfile(original, first)
            run([first, "read", item_id])
            outsider = folder / "outsider"
            shutil.copyfile(second, outsider)
            outsider.chmod(0o700)
            run(["codesign", "--force", "--sign", "-", "--identifier", "com.virty.signing-probe", outsider])
            denied = read_probe(outsider, item_id)
            assert not denied["ok"], "Unrelated build gained access"
            report = {"supported": changed["ok"] and same_path["ok"],
                      "firstSignature": first_signature, "secondSignature": second_signature,
                      "partitions": partitions, "originalReadable": True,
                      "differentPath": changed, "samePath": same_path, "adHocDenied": True}
        finally:
            try:
                if created:
                    run([first, "delete", item_id])
            finally:
                helper("delete", keychain=str(keychain), password=password)
                assert not keychain.exists(), "Temporary signing Keychain was not removed"
    assert run(["security", "list-keychains", "-d", "user"]) == original_search
    report["cleanupVerified"] = True
    evidence = ROOT / ".build/local-signing-report.json"
    evidence.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["supported"] else 1


def main():
    os.umask(0o077)
    try:
        return probe()
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
        print(f"Signing error: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
