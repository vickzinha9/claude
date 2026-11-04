"""The switch of the real execution, and the launcher's pause: two small files in runtime/.

runtime/execucao-real-armada.json exists only while the real execution is armed, for one
account (real or demo). PocketBroker reads it again before every order: removing it (Desarmar
in the launcher, or python -m pocketautonomo.arming desarmar) stops the next order, at once,
without restarting anything. Every run of the real execution starts disarmed, unless it was
started with --armar; it disarms when it ends.

runtime/pausa-solicitada exists while the launcher's Pausar is on: no new cycle starts; an
order already sent is still followed to its result.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ARM_FILE_NAME = "execucao-real-armada.json"
PAUSE_FILE_NAME = "pausa-solicitada"
ACCOUNTS = ("real", "demo")


class ArmSwitch:
    def __init__(self, runtime_dir, account):
        if account not in ACCOUNTS:
            raise ValueError("account must be real or demo")
        self.path = Path(runtime_dir) / ARM_FILE_NAME
        self.pause_path = Path(runtime_dir) / PAUSE_FILE_NAME
        self.account = account

    def __call__(self):
        """(True, None) while armed for this account, else (False, why)."""
        state = read_state(self.path)
        if state is None:
            return False, "o botão Armar não está ligado"
        if state.get("account") != self.account:
            return False, f"armada para a conta {state.get('account')}, e esta execução é da conta {self.account}"
        return True, None

    def arm(self, by):
        arm(self.path.parent, self.account, by)

    def disarm(self):
        disarm(self.path.parent)

    def gate(self):
        """Why no new cycle may start now, or None: disarmed, or paused in the launcher."""
        if self.pause_path.exists():
            return "pausa pedida no painel: nenhum ciclo novo começa"
        armed, why = self()
        return None if armed else f"execução real desarmada: {why}; nenhuma ordem é enviada"


def read_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def arm(runtime_dir, account, by):
    if account not in ACCOUNTS:
        raise ValueError("account must be real or demo")
    path = Path(runtime_dir) / ARM_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"account": account, "armed_at": datetime.now(timezone.utc).isoformat(), "by": by}),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def disarm(runtime_dir):
    try:
        (Path(runtime_dir) / ARM_FILE_NAME).unlink()
    except FileNotFoundError:
        pass


def set_pause(runtime_dir, paused):
    path = Path(runtime_dir) / PAUSE_FILE_NAME
    if paused:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def main(argv=None):
    from pocketautonomo.observe import RUNTIME_DIR

    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["armar"] and args[1:2] and args[1] in ACCOUNTS:
        arm(RUNTIME_DIR, args[1], "terminal")
        print(f"Execução real ARMADA na conta {args[1]}: a próxima decisão CALL/PUT válida envia ordem.")
        return 0
    if args[:1] == ["desarmar"]:
        disarm(RUNTIME_DIR)
        print("Execução real DESARMADA: nenhuma ordem é enviada.")
        return 0
    state = read_state(RUNTIME_DIR / ARM_FILE_NAME)
    print(f"Estado: {'ARMADA na conta ' + state.get('account', '?') if state else 'DESARMADA'}")
    print("Uso: python -m pocketautonomo.arming armar real|demo  |  python -m pocketautonomo.arming desarmar")
    return 0 if not args else 2


if __name__ == "__main__":
    sys.exit(main())
