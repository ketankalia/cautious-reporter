Step 1 — Activate the virtual environment

cd d:\DEVWORK\python_projects\Reporter
.venv\Scripts\Activate.ps1
Step 2 — Install PyInstaller

pip install pyinstaller
Step 3 — Generate the .spec files

pyinstaller --onefile --name reporter report_comparator.py
pyinstaller --onefile --name html_reporter html_reporter.py
This creates reporter.spec and html_reporter.spec in the project root. Ignore the dist/ and build/ folders it also creates — we'll rebuild from the fixed spec files next.

Step 4 — Edit reporter.spec
Open reporter.spec and find the Analysis(...) block. Add the hiddenimports list:


a = Analysis(
    ['report_comparator.py'],
    ...
    hiddenimports=[
        'pandas',
        'pandas._libs.tslibs.np_datetime',
        'pandas._libs.tslibs.nattype',
        'pandas._libs.tslibs.timedeltas',
        'pandas._libs.tslibs.timestamps',
        'pandas._libs.hashtable',
        'pandas._libs.lib',
        'pandas._libs.missing',
        'sklearn',
        'sklearn.feature_extraction',
        'sklearn.feature_extraction.text',
        'sklearn.metrics.pairwise',
        'sklearn.utils._cython_blas',
        'sklearn.neighbors.typedefs',
        'sklearn.neighbors.quad_tree',
        'sklearn.tree._utils',
        'scipy',
        'scipy.sparse',
        'scipy.sparse.csgraph',
    ],
    ...
)
Step 5 — Edit html_reporter.spec
Same change — add the identical hiddenimports list to the Analysis(...) block in html_reporter.spec.

Step 6 — Build both executables from the spec files

pyinstaller reporter.spec
pyinstaller html_reporter.spec
Both .exe files will appear in dist\.

Step 7 — Test

dist\reporter.exe settlement_a.txt settlement_b.txt

dist\reporter.exe settlements_a\ settlements_b\ --output-dir results\

dist\html_reporter.exe settlements_a\ settlements_b\ --output results\report.html

dist\html_reporter.exe settlements_a\ settlements_b\ --output results\report.html --semantic
The --semantic test is important — it exercises the scikit-learn hidden imports.

Step 8 — Package for distribution
Create a folder and copy the files in:


mkdir reporter_v1.0_windows
copy dist\reporter.exe reporter_v1.0_windows\
copy dist\html_reporter.exe reporter_v1.0_windows\
copy USAGE.md reporter_v1.0_windows\
Then zip reporter_v1.0_windows\ and share it.

Troubleshooting
If the exe crashes with ModuleNotFoundError:
Add the missing module name to the hiddenimports list in the .spec file and rebuild with pyinstaller reporter.spec.

If antivirus blocks the exe:
This is common with PyInstaller. Code-signing the exe resolves it, or users can add an exclusion.

If the build fails entirely:
Run with --log-level DEBUG to see details:


pyinstaller --log-level DEBUG reporter.spec
