# ملاحظات الإصدار — أكاديمية يوم جديد (`Yawm Jadid Academy Suite v1.2.0 — Session 12`)

**تاريخ الإصدار المرجعي**: `2026-11-15`  
**رقم الإصدار**: `1.2.0` (`Git Commit: d5467f2+`)  
**عدد الاختبارات الآلية المجتازة**: `318` اختباراً (`310 PASS`، `1 Skipped`، `7 Expected Failures` محفوظة لقرارات السياسة المالية، `0 Failures`)

## أبرز التحديثات والإصلاحات الهندسية في الإصدار 1.2.0 (حتى الجلسة 12)

1. **مواءمة شاملة لعقود الواجهة الأمامية والخادم (`UI ↔ API Full Contract Parity`)**:
   - إصلاح التعديل الجزئي لخلايا الشهر (`PATCH /api/students/{id}/months/{mk}`) في `database.py::upsert_student_month` ليدمج الحقول المعدلة مع بيانات الشهر الموجودة في قاعدة البيانات بدلاً من تصفير قيمة الاشتراك وتاريخ الدفع.
   - دعم كامل لمسارات الواجهة الأمامية في `server.py`: `PUT /api/students/{id}`، `POST /api/onboarding/complete` (مع دعم قوائم الباقات `subTypes` ونسب المعلمات)، `PUT /api/settings`، `GET /api/backups/health`، `PATCH /api/sessions/{id}`، `POST /api/sessions/{id}/compensate`، و`PATCH /api/madrasa/enrollments/{id}`.
   - توحيد هياكل استجابة `/api/alerts` و`/api/statistics` ودعم تأجيل التنبيهات عبر `alertId` و`snoozeDays`.
   - إصلاح ترتيب معاملات `FE.generateDynamicMonths` في `store.js` و`finance-engine.js` لضمان ظهور كافة الشهور المسجلة في قوائم التصفية.
2. **الأمان وتحصين الخادم المحلي (`Phase 1 & Session 12 Security Hardening`)**:
   - إضافة طبقة `MaxBodySizeASGIMiddleware` على مستوى ASGI لفرض حد أقصى (`25 MB`) حتى على الطلبات المتدفقة (`Chunked Transfer-Encoding`) بدون ترويسة `Content-Length` (`F-09`).
   - منع تسريب تفاصيل استثناءات `openpyxl`/`zipfile` الداخلية عند رفع ملف إكسيل تالف (`SEC-1`).
   - قصر قبول رمز النافذة `?wsec=` على مسارات الإقلاع فقط وحذفه فوراً من شريط العنوان عبر `history.replaceState` (`F-03`).
   - تحييد المعادلات (`Formula Injection Neutralization`) في كافة ملفات التصدير (`.xlsx` و`.csv`) وتقارير أخطاء الاستيراد (`IMPORT-1`).
3. **سلامة البيانات والنسخ الاحتياطي (`WAL & Backup Resilience`)**:
   - تنظيف ملفات `-wal` و`-shm` المرافقة للنسخ المؤقتة (`DEFECT-B1`) والتراجع الذري التلقائي عند محاولة استعادة نسخة غير متوافقة (`F-01`).
   - تسجيل تحذيرات واضحة في السجل عند تعذر الوصول إلى مجلد النسخ الإضافي (`F-15`) وعرض حالته في شاشة صحة النظام (`F-13`).
   - تجميد الأشهر المقفلة محاسبياً (`MonthLockedError` -> HTTP 400 `MONTH_LOCKED`) وأرشفة المعلمات بدلاً من حذفهن عند وجود سجلات مرتبطة.
4. **أداة الفحص الجنائي لملفات البناء (`scripts/verify_build_artifact.py`)**:
   - فحص آلي لملف `YawmJadidAcademy.exe` بعد البناء للتأكد من معمارية 64-بت، وإصدار بايثون 3.12، وتطابق جميع الـ 20 موديول بايثون وملفات الواجهة الـ 29 بالبصمة (`SHA-256`)، وخلو الملف التنفيذي من أي بيانات طالبات أو قواعد بيانات مدمجة (`PRIV-1` / `BUILD-1`).
