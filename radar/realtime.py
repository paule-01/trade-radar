"""Einstiegspunkt Sofort-Push: eToro + wikifolio (läuft alle ~10 Minuten)."""
from . import etoro, wikifolio
from .common import load_config, log


def main() -> None:
    cfg = load_config()
    risk_cfg = cfg.get("risk", {})
    for name, mod in (("etoro", etoro), ("wikifolio", wikifolio)):
        try:
            mod.run(cfg.get(name, {}), risk_cfg)
        except Exception:
            log.exception("%s fehlgeschlagen", name)


if __name__ == "__main__":
    main()
