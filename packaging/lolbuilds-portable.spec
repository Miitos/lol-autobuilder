# -*- mode: python ; coding: utf-8 -*-
# Recette PyInstaller de l'exe PORTABLE : un seul fichier lolbuilds-portable.exe (dist/lolbuilds-portable.exe),
# à copier où l'on veut, sans dossier _internal ni zip à décompresser. Utilisée en local et par la CI :
#   pyinstaller --noconfirm --clean packaging/lolbuilds-portable.spec
#
# Même contenu que packaging/lolbuilds.spec (qui construit le dossier embarqué dans l'installateur) ; seul
# l'empaquetage change. Un exe seul s'extrait dans un dossier temporaire à chaque lancement, donc rien n'est
# bloqué par la « marque du Web » (Zone.Identifier) que Windows pose sur les fichiers d'un zip téléchargé : c'est
# ce qui faisait planter pythonnet (« Failed to resolve Python.Runtime.Loader.Initialize ») au démarrage.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent

datas = [(str(root / "src" / "lolbuilds" / "app" / "ui"), "lolbuilds/app/ui")]  # HTML/CSS/JS + polices
binaries = []
hiddenimports = []
for package in ("webview",):  # pywebview : DLL WebView2 et modules chargés dynamiquement
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lolbuilds-portable",
    icon=str(root / "packaging" / "lolbuilds.ico"),
    console=False,  # application fenêtrée : pas de console noire
    upx=False,  # UPX augmente les faux positifs des antivirus
)
