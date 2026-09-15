> Explicit synthetic fault injection. Not live model output.

# Fund Facts Cross-Check

Synthetic documents only. Research only; never auto-act.

Request: 738ad1a1-77af-4df0-b741-fd1367648162 | Execution: complete

Execution completeness and model agreement are independent of evidence support.

| Fund / class / effective date | Field | Model A | Model B | Relation | A evidence | B evidence |
| --- | --- | --- | --- | --- | --- | --- |
| alpha / A / 2026-01-01 | annual_management_fee | {&quot;amount&quot;: &quot;0.30&quot;, &quot;unit&quot;: &quot;percent&quot;} | {&quot;amount&quot;: &quot;30&quot;, &quot;unit&quot;: &quot;bps&quot;} | agreement | supported: 原文支持该字段值。 Sources: alpha-v1/management_fee: The annual management fee for Class A is 0.30%.; checked: alpha-v1/capital_guarantee, alpha-v1/management_fee | supported: 原文支持该字段值。 Sources: alpha-v1/management_fee: The annual management fee for Class A is 0.30%.; checked: alpha-v1/capital_guarantee, alpha-v1/management_fee |
| alpha / A / 2026-01-01 | capital_guarantee | {&quot;label&quot;: &quot;guaranteed&quot;} | {&quot;label&quot;: &quot;guaranteed&quot;} | agreement | contradicted: 模型的本金保证结论与原文相反。 Sources: alpha-v1/capital_guarantee: Capital is not guaranteed. Investors may lose part or all of their investment.; checked: alpha-v1/capital_guarantee, alpha-v1/management_fee | contradicted: 模型的本金保证结论与原文相反。 Sources: alpha-v1/capital_guarantee: Capital is not guaranteed. Investors may lose part or all of their investment.; checked: alpha-v1/capital_guarantee, alpha-v1/management_fee |
| beta / A / 2026-01-01 | annual_management_fee | {&quot;amount&quot;: &quot;45&quot;, &quot;unit&quot;: &quot;bps&quot;} | {&quot;amount&quot;: &quot;45&quot;, &quot;unit&quot;: &quot;bps&quot;} | agreement | supported: 原文支持该字段值。 Sources: beta-v1/management_fee: The annual management fee for Class A is 45 basis points.; checked: beta-v1/management_fee, beta-v1/risk | supported: 原文支持该字段值。 Sources: beta-v1/management_fee: The annual management fee for Class A is 45 basis points.; checked: beta-v1/management_fee, beta-v1/risk |
| beta / A / 2026-01-01 | capital_guarantee | not_stated | not_stated | both_unknown | supported: 检查范围内未找到声明。 Sources: none; checked: beta-v1/management_fee, beta-v1/risk | supported: 检查范围内未找到声明。 Sources: none; checked: beta-v1/management_fee, beta-v1/risk |

## Model calls

- a: fixture/model-a — ok; attempts=1; tokens=None/None (null = unavailable)
- b: fixture/model-b — ok; attempts=1; tokens=None/None (null = unavailable)
