import json
import base64
import fnmatch
import threading
import tempfile
import os
from pathlib import Path
from typing import Any, List, Optional

class SimpleStorage:
    """Shared storage system for agent communication with safe key encoding and thread safety."""
    
    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            base_dir = os.getenv("PEON_STORAGE", ".peon-orchestrator/storage")
        self.base_dir = Path(base_dir).absolute()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
    
    def _encode_key(self, key: str) -> str:
        """Encode key to a filesystem-safe string using URL-safe base64."""
        return base64.urlsafe_b64encode(key.encode()).decode().rstrip('=')
    
    def _decode_key(self, encoded: str) -> str:
        """Decode filesystem-safe string back to original key."""
        padding = '=' * (4 - len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded + padding).decode()

    def write(self, key: str, value: Any) -> None:
        """Atomic write of a JSON-serializable value."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        
        file_path = self.base_dir / f"{self._encode_key(key)}.json"
        
        with self._lock:
            # Use a temporary file for atomic write
            fd, temp_path = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(value, f, ensure_ascii=False, indent=2)
                # Atomic replace
                os.replace(temp_path, file_path)
            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e
    
    def read(self, key: str) -> Any:
        """Thread-safe read of a value from storage."""
        file_path = self.base_dir / f"{self._encode_key(key)}.json"
        with self._lock:
            if not file_path.exists():
                return None
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    
    def delete(self, key: str) -> bool:
        """Thread-safe deletion of a specific key."""
        file_path = self.base_dir / f"{self._encode_key(key)}.json"
        with self._lock:
            if file_path.exists():
                file_path.unlink()
                return True
            return False

    def list_keys(self) -> List[str]:
        """List all original keys in storage."""
        keys = []
        with self._lock:
            for file_path in self.base_dir.glob("*.json"):
                if file_path.suffix == ".json" and not file_path.name.startswith("."):
                    try:
                        keys.append(self._decode_key(file_path.stem))
                    except Exception:
                        keys.append(file_path.stem)
        return keys

    def cleanup(self, pattern: str = "*", exclude: Optional[str] = None) -> int:
        """Remove keys matching a glob pattern with optional exclusion."""
        count = 0
        for key in self.list_keys():
            if fnmatch.fnmatch(key, pattern):
                if exclude and fnmatch.fnmatch(key, exclude):
                    continue
                if self.delete(key):
                    count += 1
        return count
