"""Config dos agentes para o daily call pipeline."""
import shutil
from pathlib import Path

# Raiz do repo (daily_call_pipeline/). Tudo é resolvido relativo a este ficheiro,
# pelo que mover a pasta inteira não exige edição.
BASE = Path(__file__).resolve().parent
AGENTS_DIR = BASE / "agents"

_CC_KARMA = [
    "andre.nascimento@karma-network.com",
    "luis.correia@karma-network.com",
    "rui@karma-network.com",
]

AGENTS = [
    {"name": "ruben",   "display": "Rúben",   "owner_id": "80959433",
     "folder": "Ruben_Call_Report",
     "data_dir": AGENTS_DIR / "Ruben_Call_Report" / "data",
     "report_dir": AGENTS_DIR / "Ruben_Call_Report" / "reports",
     "report_display": "Rúben @ NOVA Digital",
     "business": "nova_digital",
     "email_to": "ruben.correia@novadigital.pt", "email_cc": _CC_KARMA},
    {"name": "david",   "display": "David",   "owner_id": "80910786",
     "folder": "David_Call_Report",
     "data_dir": AGENTS_DIR / "David_Call_Report" / "data",
     "report_dir": AGENTS_DIR / "David_Call_Report" / "reports",
     "report_display": "David Chaves @ Real Estate",
     "business": "real_estate",
     "email_to": "sandra@realestateinvest.pt", "email_cc": _CC_KARMA + ["david.chaves@realestateinvest.pt"]},
    {"name": "sandra",  "display": "Sandra",  "owner_id": "84097204",
     "folder": "Sandra_Call_Report",
     "data_dir": AGENTS_DIR / "Sandra_Call_Report" / "data",
     "report_dir": AGENTS_DIR / "Sandra_Call_Report" / "reports",
     "report_display": "Sandra Hartmann @ Real Estate",
     "business": "real_estate",
     "email_to": "sandra@realestateinvest.pt", "email_cc": _CC_KARMA},
    {"name": "emanuel", "display": "Emanuel", "owner_id": "89315069",
     "folder": "Emanuel_Call_Report",
     "data_dir": AGENTS_DIR / "Emanuel_Call_Report" / "data",
     "report_dir": AGENTS_DIR / "Emanuel_Call_Report" / "reports",
     "report_display": "Emanuel @ NOVA Digital",
     "business": "nova_digital",
     "email_to": "emanuel.martins@novadigital.pt", "email_cc": _CC_KARMA},
    {"name": "ze",      "display": "Zé",      "owner_id": "1891530472",
     "folder": "Ze_Call_Report",
     "data_dir": AGENTS_DIR / "Ze_Call_Report" / "data",
     "report_dir": AGENTS_DIR / "Ze_Call_Report" / "reports",
     "report_display": "José Santos @ Real Estate",
     "business": "real_estate",
     "email_to": "sandra@realestateinvest.pt", "email_cc": _CC_KARMA + ["jose.santos@realestateinvest.pt"]},
    {"name": "joana",   "display": "Joana",   "owner_id": "91636914",
     "folder": "Joana_Call_Report",
     "data_dir": AGENTS_DIR / "Joana_Call_Report" / "data",
     "report_dir": AGENTS_DIR / "Joana_Call_Report" / "reports",
     "report_display": "Joana Alves @ Real Estate",
     "business": "real_estate",
     "email_to": "sandra@realestateinvest.pt", "email_cc": _CC_KARMA + ["joana.alves@realestateinvest.pt"]},
    {"name": "emanuel_re", "display": "Emanuel Martins", "owner_id": "84330275",
     "folder": "Emanuel_RE_Call_Report",
     "data_dir": AGENTS_DIR / "Emanuel_RE_Call_Report" / "data",
     "report_dir": AGENTS_DIR / "Emanuel_RE_Call_Report" / "reports",
     "report_display": "Emanuel Martins @ Real Estate",
     "business": "real_estate",
     "email_to": "sandra@realestateinvest.pt",
     "email_cc": ["andre.nascimento@karma-network.com", "emanuel.martins@realestateinvest.pt"]},
    {"name": "ruben_funeral", "display": "Rúben (Funeral em Vida)", "owner_id": "33660815",
     "folder": "Ruben_Funeral_Call_Report",
     "data_dir": AGENTS_DIR / "Ruben_Funeral_Call_Report" / "data",
     "report_dir": AGENTS_DIR / "Ruben_Funeral_Call_Report" / "reports",
     "report_display": "Rúben @ Funerárias Unidas",
     "business": "funeral_em_vida",
     "hubspot_token_env": "HUBSPOT_TOKEN_FUNERAL",
     "email_to": "ruben.correia@funerariasunidas.pt", "email_cc": _CC_KARMA},
]


def _resolve_bin(name: str) -> str:
    """Locate an ffmpeg/ffprobe binary, preferring Homebrew on macOS, system path on Linux."""
    for candidate in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
        if Path(candidate).exists():
            return candidate
    found = shutil.which(name)
    return found or name  # last resort: rely on PATH at runtime


FFMPEG_BIN = _resolve_bin("ffmpeg")
FFPROBE_BIN = _resolve_bin("ffprobe")
