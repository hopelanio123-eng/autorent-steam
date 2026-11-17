from pathlib import Path


root = Path(SPECPATH)
analysis = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    datas=[(str(root / "app" / "web"), "app/web")],
    hiddenimports=[
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.lifespan.on",
    ],
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="AutoRent", console=True)
collection = COLLECT(executable, analysis.binaries, analysis.datas, name="AutoRent")
