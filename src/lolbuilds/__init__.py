"""lolbuilds : remplaçant léger de Blitz pour l'import automatique de builds LoL."""

__version__ = "0.5.5"

# Rôles canoniques (format LCU `assignedPosition`, en minuscules).
ROLES = ("top", "jungle", "middle", "bottom", "utility")

# Pseudo-rôle des builds ARAM (pas de lane en ARAM) : `{Champion}_aram.json`. Hors de ROLES : ce n'est pas une lane.
ARAM = "aram"

# Libellés courts utilisés dans les titres de pages de runes / item sets.
ROLE_LABELS = {
    "aram": "ARAM",
    "top": "Top",
    "jungle": "Jungle",
    "middle": "Mid",
    "bottom": "ADC",
    "utility": "Support",
}

# Préfixe des objets (pages de runes, item sets) créés par l'outil.
AUTO_PREFIX = "[Auto]"
