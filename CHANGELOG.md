## [0.3.0](https://github.com/foro-sh/exact-online-mcp/compare/v0.2.1...v0.3.0) (2026-11-11)

### Features

* act as each caller's own Exact user with a per-user grant ([2477654](https://github.com/foro-sh/exact-online-mcp/commit/247765459a9aa4c65d080395c86cdc3862c1ba86)), closes [foro-sh/platform#1722](https://github.com/foro-sh/platform/issues/1722)

## [0.2.1](https://github.com/foro-sh/exact-online-mcp/compare/v0.2.0...v0.2.1) (2026-11-11)

### Bug Fixes

* declare the python runtime so foro can deploy with package.json present ([16f1985](https://github.com/foro-sh/exact-online-mcp/commit/16f1985c5ebb1bb6eeb92c29c068c08f50e6d262))

## [0.2.0](https://github.com/foro-sh/exact-online-mcp/compare/v0.1.0...v0.2.0) (2026-11-10)

### Features

* list every tool and let foro policy decide ([b55eeb6](https://github.com/foro-sh/exact-online-mcp/commit/b55eeb677eee367f332f1387418eea2221df39d5))

## [0.1.0](https://github.com/foro-sh/exact-online-mcp/compare/v0.0.0...v0.1.0) (2026-11-10)

### ⚠ BREAKING CHANGES

* a deploy configured with TOKEN_STORE_URL and
TOKEN_STORE_KEY must run connect again and add the `exact` grant.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>

### Features

* add a bookkeeping toolset that books and matches entries ([f9b79b3](https://github.com/foro-sh/exact-online-mcp/commit/f9b79b387748ca1bf74fc546698ce96ba488461c))
* add a scoped query toolset for the resources no tool covers ([e39f0b0](https://github.com/foro-sh/exact-online-mcp/commit/e39f0b0f06f571426d402f942f89f93fb34a7b8c))
* add balance sheet, cash position, budget and payment behaviour tools ([d29a748](https://github.com/foro-sh/exact-online-mcp/commit/d29a74891c557154bcae21b1fcd97c6a843cbcad))
* add exact online client with rotation-safe token store ([df0fc32](https://github.com/foro-sh/exact-online-mcp/commit/df0fc32769dcd5f4faecd213fe1b7fcd3953a3a3))
* add ledger, purchase and report tools ([249a487](https://github.com/foro-sh/exact-online-mcp/commit/249a487f919cdf0e8188481393c1d9c071b0c9ed))
* add one-time connect cli to seed the token store ([fa6ff81](https://github.com/foro-sh/exact-online-mcp/commit/fa6ff81a87deae9a17fd916b72dc49c6647cded6))
* add toolsets for orders, projects, crm, subscriptions, documents and changes ([87f571e](https://github.com/foro-sh/exact-online-mcp/commit/87f571e7a95f4815f1c3c069299fc2a8412a95f6))
* **client:** post records and xml uploads to exact ([c4c93a4](https://github.com/foro-sh/exact-online-mcp/commit/c4c93a44721e25073329a08ea215685e813dd6dc))
* connect through foro's redirect instead of a local command ([31e6094](https://github.com/foro-sh/exact-online-mcp/commit/31e609480c74a2d03a6e3aecce3d5dd5685c366b)), closes [foro-sh/platform#1645](https://github.com/foro-sh/platform/issues/1645)
* declare the exact grant so foro prefills Connect ([f9cd7b0](https://github.com/foro-sh/exact-online-mcp/commit/f9cd7b033ba731bf1292d43255f60cd84a3635e9)), closes [foro-sh/platform#1654](https://github.com/foro-sh/platform/issues/1654)
* expose read-only accounting tools over streamable http ([e83d845](https://github.com/foro-sh/exact-online-mcp/commit/e83d8452b878b52350b6808284d8dbfffa92cb91))
* get exact tokens from foro's upstream broker ([738ba9c](https://github.com/foro-sh/exact-online-mcp/commit/738ba9c7863bdfbe3071981b0d853ee9139fcafb))
* pass query parameters to read functions and skip $top on bulk endpoints ([99ecec7](https://github.com/foro-sh/exact-online-mcp/commit/99ecec72e09b509285857caec41b492144340cb9))

### Bug Fixes

* **bookkeeping:** close gaps found researching exact's api ([71fc628](https://github.com/foro-sh/exact-online-mcp/commit/71fc628783fcd556bcb373d6717bf86a01ca54b7))
* **bookkeeping:** close gaps found testing against a live company ([286cbf0](https://github.com/foro-sh/exact-online-mcp/commit/286cbf0f72194e07395536f5143bc65bde425e5d))
* **bookkeeping:** list every unmatched payment, and steer the booking prompt ([80efb44](https://github.com/foro-sh/exact-online-mcp/commit/80efb44b9ba0482901e231aec1c0e0d6a8421c4f))
* **bookkeeping:** send vat codes padded as exact holds them ([329d9d2](https://github.com/foro-sh/exact-online-mcp/commit/329d9d2ced061be109dce52acd81d6974601f130))
* bump certifi to 2026.10.23 (GHSA-6q48-6c7w-9gw2) ([f8e97cb](https://github.com/foro-sh/exact-online-mcp/commit/f8e97cb41444d84ed15d9c69c9cac6047bc9070a))
* catch in the server what live testing showed only exact refusing ([6ee38c2](https://github.com/foro-sh/exact-online-mcp/commit/6ee38c2a0c8f211b11dd8bf45886ce5eca41bdef))
* **documents:** download attachments from their url, as the listing's field is empty ([9b64233](https://github.com/foro-sh/exact-online-mcp/commit/9b64233bc23451c040dc3d6b9be773798e98c395))
* round VAT as an invoice does, and close gaps found in review ([67fd41e](https://github.com/foro-sh/exact-online-mcp/commit/67fd41e825cc945ee265d4ab7cd7d8779830b813))
* tell per-minute from daily rate limits without a reset header ([d92b0ad](https://github.com/foro-sh/exact-online-mcp/commit/d92b0adb3a47016b590a22793432a5c584d3102c))
* **tokens:** keep a broker token with no expiry until Exact refuses it ([fca3c3c](https://github.com/foro-sh/exact-online-mcp/commit/fca3c3cbec28da65adab501cfab013be92e937b0)), closes [foro-sh/platform#1676](https://github.com/foro-sh/platform/issues/1676)
