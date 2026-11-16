# أكاديمية يوم جديد — Yawm Jadid Academy Suite (v1.2.0)

برنامج إدارة أوفلاين لأكاديمية قرآن (طالبات، اشتراكات، إيصالات، أجور معلمات، حضور، حفظ، شهادات، تنبيهات، استيراد/تصدير Excel، نسخ احتياطي).
Python 3.12 · FastAPI (loopback) · SQLite WAL · JS خام · PyWebView · PyInstaller. اسم الحزمة الداخلي `alahd_erp` وفولدر البيانات `AlAhdAcademySuite` ثابتين عشان التثبيتات القديمة.

**بناء + تشغيل بضغطة واحدة (EXE جاهز):** دبل كليك على `build_and_run.bat` — بيعمل venv، يسطّب المكتبات، يشغّل الاختبارات، يبني `YawmJadidAcademy.exe`، يتحقق منه، يعمل Smoke test، يطلّع `release\YawmJadidAcademy-1.2.0-portable.zip`، وبعدين يفتح البرنامج. خيارات: `/fast` (من غير اختبارات) · `/run` (تشغيل الـ EXE المبني) · `/dev` (تشغيل من المصدر) · `/strict` (يوقف البناء لو أي اختبار فشل؛ الافتراضي تحذير وكمّل).

**ملاحظة مهمة لو الـ .bat مش بيشتغل:** لازم الملفات تفضل ASCII + CRLF (فيه اختبار `tests/test_build_scripts.py` بيحمي ده). مكتبة `pywebview` اختيارية: لو فشل تسطيبها البرنامج بيفتح في المتصفح.

**أسهل طريقة للتشغيل على ويندوز (من غير بناء EXE):** دوس دبل كليك على `START_ACADEMY.bat` (بيجهّز كل حاجة أول مرة بس، وبعدها بيفتح البرنامج أوفلاين).

```
python -m venv .venv && .venv\Scripts\activate
pip install -e .[dev]
python -m unittest discover -s tests -t .      # الاختبارات
python -m alahd_erp.main                        # تشغيل من المصدر
build_and_run.bat                               # بناء + تحقق + تشغيل 
```

التوثيق في [`docs/`](docs/): `ARCHITECTURE` · `ISSUES` · `TESTING` · `DEVELOPMENT` · `RELEASE_CHECKLIST`.
