---
description: Tailor my resume for a job posting URL, compile it, open the application in Chrome, and log it
---
The user gives a job-posting URL or pasted JD as: $ARGUMENTS

1. **Get the target.** WebFetch the URL (fallback `curl -sL`). Extract company, exact title,
   location, cycle, required + preferred qualifications, and any short-answer questions.
2. **Load my context.** Read `resumes/PROFILE.md` (my master bullet bank: every bullet I've
   used, tagged verified / needs-confirming / never-use) and `applications.md` (my tracker).
   Rules: NEVER invent metrics or claims. If a preferred qual is almost-true, tell me the
   small task that would make it true instead of writing it. No em-dashes. Plain human voice.
3. **Pick the base** `resumes/base/<lane>.tex` (e.g. swe.tex vs research.tex).
4. **Tailor with minimal edits**: reorder / bold / swap bullets from the bank, don't rewrite.
   Cover every truthfully-claimable preferred qualification and **bold** those phrases.
   Save as `resumes/out/<Company>/<Lastname>_<First>_<Company>.tex`. Never overwrite a base.
5. **Compile** with `tectonic <file>.tex` (brew install tectonic). Must be exactly 1 page,
   no overfull lines. Fix and recompile until clean. Open the PDF in Finder (`open -R`).
6. **Coverage table**: each JD requirement -> where the resume now addresses it, or "gap".
7. **Draft short-answer / essay questions** in my voice. Show as drafts; never auto-submit.
8. **Open the application in Chrome** (Claude in Chrome extension): navigate to the apply
   URL, fill every text/select field from `applications.md` -> "form defaults" (name, school,
   grad year, GPA, work authorization, sponsorship = No, EEO answers). Claude cannot upload
   files, so stop at the resume-upload field and tell me which PDF to attach. NEVER click
   Submit; I review and submit.
9. **Log it** in `applications.md`: company, role, status `prepared`, resume filename, date,
   deadline. When I say "applied", flip the status.
