# Ownership and legal review

This checklist records the requested ownership and publication review. It separates
the project's chosen license from questions that depend on jurisdiction, vendor
agreements and the exact distribution. It is not a conclusion that every use or
distribution is permitted.

## Project position

Independently authored application code and documentation are offered under MIT.
Each contributor retains copyright in their work; the collective attribution does
not transfer ownership. The project claims no ownership of Radmin software,
protocol rights, vendor documentation, artwork or trademarks. Original vendor
binaries are not distributed. Use requires a separate Radmin Server license and
appropriate permissions. See [NOTICE](../NOTICE).

## Before publication or commercial distribution

- [ ] **Authorship and provenance:** establish that contributors can license their
  work; identify copied third-party material and remove material without adequate
  redistribution rights. An MIT header cannot cure missing ownership.
- [ ] **Jurisdiction:** review applicable interoperability/reverse-engineering
  exceptions and limits, including anti-circumvention, copyright and any relevant
  patent issues in the places where development and distribution occur.
- [ ] **Vendor EULA and access:** review the actual Radmin agreements applicable to
  the copies and activities involved. Distinguish contractual terms from statutory
  rights; obtain jurisdiction-specific advice where that distinction matters.
- [ ] **Trademarks and presentation:** review the project name and descriptive
  compatibility references for confusion, include the unofficial/non-endorsement
  notice, and keep the independent icon. Do not use native vendor artwork or imply
  that the vendor approved or certified the client.
- [ ] **Dependencies:** review exact Qt/PySide6 modules and plugins, LGPL/GPL options,
  PyInstaller's bootloader exception, native libraries and other shipped licenses.
  Preserve notices, required source availability and library replacement rights.
- [ ] **Distribution claims:** verify that server licensing is separate, supported
  features match evidence, and download/release statements describe actual tested
  artifacts. Review applicable local distribution requirements for the final package.

Unresolved items are release-review questions, not additional restrictions inserted
into the MIT license. Keep legal advice, private agreements and confidential
research outside the public repository; publish only the conclusions and notices
needed for users and redistribution.
