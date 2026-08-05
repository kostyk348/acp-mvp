"""Docker-based sandbox executor for CI compile contracts."""

import subprocess
import json
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("acp.sandbox")


class SandboxExecutor:
    """Executes contract verification in an isolated Docker container."""

    def __init__(
        self,
        sandbox_base: str = "/tmp/agent-sandbox",
        docker_image: str = "acp-rust-sandbox:latest",
        cpu_limit: int = 1,
        mem_limit: str = "512m",
        timeout: int = 300,
    ):
        self.sandbox_base = Path(sandbox_base)
        self.docker_image = docker_image
        self.cpu_limit = cpu_limit
        self.mem_limit = mem_limit
        self.timeout = timeout

    def execute(
        self,
        source_ref: str,
        language: str,
        tests: list[str],
        timeout: int,
        work_dir: Path,
    ) -> dict[str, Any]:
        """Execute verification commands in sandbox.

        Returns dict with:
          - success: bool
          - exit_codes: list[int]
          - exec_hash: str (SHA256 of combined stdout+stderr)
          - proof: dict
          - artifact: str | None (path to output artifact)
          - stderr: str
        """
        work_dir.mkdir(parents=True, exist_ok=True)

        # Fetch source code
        source_path = self._fetch_source(source_ref, work_dir)
        if source_path is None:
            return {
                "success": False,
                "exit_codes": [],
                "exec_hash": "",
                "proof": {"tests_passed": 0, "execution_time_ms": 0, "exit_codes": []},
                "artifact": None,
                "stderr": f"Failed to fetch source from {source_ref}",
            }

        # Build Docker command
        container_name = f"acp-{work_dir.name}"
        docker_cmd = self._build_docker_cmd(
            container_name=container_name,
            work_dir=work_dir,
            source_path=source_path,
            language=language,
            tests=tests,
            timeout=timeout,
        )

        start_time = time.time()
        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 30,  # Buffer for Docker overhead
            )
            elapsed_ms = int((time.time() - start_time) * 1000)

            # Compute execution hash
            combined = result.stdout + result.stderr
            exec_hash = hashlib.sha256(combined.encode()).hexdigest()

            # Parse exit codes from output (last lines should contain them)
            exit_codes = self._extract_exit_codes(result.stdout, result.stderr)
            all_success = all(code == 0 for code in exit_codes) if exit_codes else result.returncode == 0

            # Find artifact
            artifact = self._find_artifact(work_dir)

            proof = {
                "tests_passed": len([c for c in exit_codes if c == 0]) if exit_codes else (1 if all_success else 0),
                "execution_time_ms": elapsed_ms,
                "exit_codes": exit_codes if exit_codes else [result.returncode],
            }

            return {
                "success": all_success,
                "exit_codes": exit_codes if exit_codes else [result.returncode],
                "exec_hash": exec_hash,
                "proof": proof,
                "artifact": artifact,
                "stderr": result.stderr,
                "stdout": result.stdout,
            }

        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return {
                "success": False,
                "exit_codes": [],
                "exec_hash": "",
                "proof": {
                    "tests_passed": 0,
                    "execution_time_ms": elapsed_ms,
                    "exit_codes": [-1],
                },
                "artifact": None,
                "stderr": "Execution timed out",
            }
        except Exception as e:
            logger.error(f"Sandbox execution error: {e}")
            return {
                "success": False,
                "exit_codes": [],
                "exec_hash": "",
                "proof": {"tests_passed": 0, "execution_time_ms": 0, "exit_codes": []},
                "artifact": None,
                "stderr": str(e),
            }
        finally:
            # Cleanup container
            self._cleanup_container(container_name)

    def _fetch_source(self, source_ref: str, work_dir: Path) -> Path | None:
        """Fetch source code from source_ref."""
        if source_ref.startswith("ipfs://"):
            # For MVP: simulate by creating a placeholder
            # In production, this would use ipfshttpclient
            cid = source_ref[7:]
            src_file = work_dir / "source.rs"
            src_file.write_text(f"// Source from IPFS: {cid}\nfn main() {{ println!(\"hello\"); }}\n")
            return src_file
        elif source_ref.startswith("base64://"):
            import base64
            encoded = source_ref[len("base64://"):]
            decoded = base64.b64decode(encoded)
            src_file = work_dir / "source.bin"
            src_file.write_bytes(decoded)
            return src_file
        elif Path(source_ref).exists():
            return Path(source_ref)
        else:
            # For MVP: create a minimal Rust project for testing
            return self._create_demo_project(work_dir, source_ref)

    def _create_demo_project(self, work_dir: Path, _source_ref: str) -> Path:
        """Create a minimal Rust project for testing."""
        src_dir = work_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)

        # Cargo.toml
        cargo_toml = work_dir / "Cargo.toml"
        cargo_toml.write_text(
            '[package]\nname = "acp-demo"\nversion = "0.1.0"\nedition = "2021"\n\n'
            '[lib]\ncrate-type = ["cdylib"]\n'
        )

        # lib.rs
        lib_rs = src_dir / "lib.rs"
        lib_rs.write_text(
            '#![no_std]\n\n#[no_mangle]\npub extern "C" fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n'
        )

        return work_dir

    def _build_docker_cmd(
        self,
        container_name: str,
        work_dir: Path,
        source_path: Path,
        language: str,
        tests: list[str],
        timeout: int,
    ) -> list[str]:
        """Build the docker run command."""
        # For MVP, use a simple approach: copy source into container and run commands
        # In production, this would use a pre-built image with the toolchain
        return [
            "docker", "run", "--rm",
            "--name", container_name,
            "--memory", self.mem_limit,
            "--cpus", str(self.cpu_limit),
            "--network", "none",
            "--read-only",
            "-v", f"{work_dir}:/workspace:rw",
            "-w", "/workspace",
            self.docker_image,
            "sh", "-c",
            " && ".join(tests),
        ]

    def _extract_exit_codes(self, stdout: str, stderr: str) -> list[int]:
        """Try to extract exit codes from command output."""
        codes = []
        for line in (stdout + stderr).splitlines():
            line = line.strip()
            if line.startswith("EXIT_CODE:"):
                try:
                    codes.append(int(line.split(":", 1)[1].strip()))
                except ValueError:
                    pass
        return codes

    def _find_artifact(self, work_dir: Path) -> str | None:
        """Find the compiled artifact in the work directory."""
        for pattern in ["*.wasm", "*.so", "*.a", "*.o", "target/release/*", "target/debug/*"]:
            for f in work_dir.glob(pattern):
                if f.is_file():
                    return str(f)
        return None

    def _cleanup_container(self, name: str):
        """Remove Docker container after execution."""
        try:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
