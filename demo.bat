@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  STEP 1/4  unit tests        (expect: Ran 19 tests ... OK)
echo ============================================================
python -m unittest discover -s tests
echo.

echo ============================================================
echo  STEP 2/4  clean sample      (expect: 100/100, exit code 0)
echo ============================================================
python data_lint.py examples/orders_clean.csv
echo exit code: %ERRORLEVEL%
echo.

echo ============================================================
echo  STEP 3/4  dirty sample      (expect: 42/100, exit code 1)
echo ============================================================
python data_lint.py examples/orders_dirty.csv --key order_id
echo exit code: %ERRORLEVEL%
echo.

echo ============================================================
echo  STEP 4/4  html report       (expect: report.html created)
echo ============================================================
python data_lint.py examples/orders_dirty.csv --key order_id --html report.html
if exist report.html (echo OK: report.html created, double-click it) else (echo FAIL: report.html missing)
echo.

echo ============================================================
echo  Done. Press any key to close.
echo ============================================================
pause >nul
