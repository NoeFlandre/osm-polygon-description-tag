---
pretty_name: OSM Polygon Description Tag
license: odbl
language:
- multilingual
tags:
- geospatial
- openstreetmap
- geoparquet
configs:
- config_name: default
  data_files:
  - split: train
    path: data/*.parquet
- config_name: language-v1
  data_files:
  - split: train
    path:
    - language-v1/data/afghanistan-latest.parquet
    - language-v1/data/albania-latest.parquet
    - language-v1/data/algeria-latest.parquet
    - language-v1/data/alsace-latest.parquet
    - language-v1/data/american-oceania-latest.parquet
    - language-v1/data/andalucia-latest.parquet
    - language-v1/data/andorra-latest.parquet
    - language-v1/data/angola-latest.parquet
    - language-v1/data/antarctica-latest.parquet
    - language-v1/data/aquitaine-latest.parquet
    - language-v1/data/aragon-latest.parquet
    - language-v1/data/argentina-latest.parquet
    - language-v1/data/armenia-latest.parquet
    - language-v1/data/asturias-latest.parquet
    - language-v1/data/australia-latest.parquet
    - language-v1/data/austria-latest.parquet
    - language-v1/data/auvergne-latest.parquet
    - language-v1/data/azerbaijan-latest.parquet
    - language-v1/data/azores-latest.parquet
    - language-v1/data/baden-wuerttemberg-latest.parquet
    - language-v1/data/bahamas-latest.parquet
    - language-v1/data/bangladesh-latest.parquet
    - language-v1/data/basse-normandie-latest.parquet
    - language-v1/data/bayern-latest.parquet
    - language-v1/data/belarus-latest.parquet
    - language-v1/data/belgium-latest.parquet
    - language-v1/data/belize-latest.parquet
    - language-v1/data/benin-latest.parquet
    - language-v1/data/berlin-latest.parquet
    - language-v1/data/bermuda-latest.parquet
    - language-v1/data/bhutan-latest.parquet
    - language-v1/data/bolivia-latest.parquet
    - language-v1/data/bosnia-herzegovina-latest.parquet
    - language-v1/data/botswana-latest.parquet
    - language-v1/data/bourgogne-latest.parquet
    - language-v1/data/brandenburg-latest.parquet
    - language-v1/data/brazil-centro-oeste-latest.parquet
    - language-v1/data/brazil-nordeste-latest.parquet
    - language-v1/data/brazil-norte-latest.parquet
    - language-v1/data/brazil-sudeste-latest.parquet
    - language-v1/data/brazil-sul-latest.parquet
    - language-v1/data/bremen-latest.parquet
    - language-v1/data/bretagne-latest.parquet
    - language-v1/data/bulgaria-latest.parquet
    - language-v1/data/burkina-faso-latest.parquet
    - language-v1/data/burundi-latest.parquet
    - language-v1/data/cambodia-latest.parquet
    - language-v1/data/cameroon-latest.parquet
    - language-v1/data/canada-alberta-latest.parquet
    - language-v1/data/canada-british-columbia-latest.parquet
    - language-v1/data/canada-manitoba-latest.parquet
    - language-v1/data/canada-new-brunswick-latest.parquet
    - language-v1/data/canada-newfoundland-and-labrador-latest.parquet
    - language-v1/data/canada-northwest-territories-latest.parquet
    - language-v1/data/canada-nova-scotia-latest.parquet
    - language-v1/data/canada-nunavut-latest.parquet
    - language-v1/data/canada-ontario-latest.parquet
    - language-v1/data/canada-prince-edward-island-latest.parquet
    - language-v1/data/canada-quebec-latest.parquet
    - language-v1/data/canada-saskatchewan-latest.parquet
    - language-v1/data/canada-yukon-latest.parquet
    - language-v1/data/canary-islands-latest.parquet
    - language-v1/data/cantabria-latest.parquet
    - language-v1/data/cape-verde-latest.parquet
    - language-v1/data/castilla-la-mancha-latest.parquet
    - language-v1/data/castilla-y-leon-latest.parquet
    - language-v1/data/cataluna-latest.parquet
    - language-v1/data/central-african-republic-latest.parquet
    - language-v1/data/centre-latest.parquet
    - language-v1/data/centro-latest.parquet
    - language-v1/data/ceuta-latest.parquet
    - language-v1/data/chad-latest.parquet
    - language-v1/data/champagne-ardenne-latest.parquet
    - language-v1/data/chile-latest.parquet
    - language-v1/data/china-anhui-latest.parquet
    - language-v1/data/china-beijing-latest.parquet
    - language-v1/data/china-chongqing-latest.parquet
    - language-v1/data/china-fujian-latest.parquet
    - language-v1/data/china-gansu-latest.parquet
    - language-v1/data/china-guangdong-latest.parquet
    - language-v1/data/china-guangxi-latest.parquet
    - language-v1/data/china-guizhou-latest.parquet
    - language-v1/data/china-hainan-latest.parquet
    - language-v1/data/china-hebei-latest.parquet
    - language-v1/data/china-heilongjiang-latest.parquet
    - language-v1/data/china-henan-latest.parquet
    - language-v1/data/china-hong-kong-latest.parquet
    - language-v1/data/china-hubei-latest.parquet
    - language-v1/data/china-hunan-latest.parquet
    - language-v1/data/china-inner-mongolia-latest.parquet
    - language-v1/data/china-jiangsu-latest.parquet
    - language-v1/data/china-jiangxi-latest.parquet
    - language-v1/data/china-jilin-latest.parquet
    - language-v1/data/china-liaoning-latest.parquet
    - language-v1/data/china-macau-latest.parquet
    - language-v1/data/china-ningxia-latest.parquet
    - language-v1/data/china-qinghai-latest.parquet
    - language-v1/data/china-shaanxi-latest.parquet
    - language-v1/data/china-shandong-latest.parquet
    - language-v1/data/china-shanghai-latest.parquet
    - language-v1/data/china-shanxi-latest.parquet
    - language-v1/data/china-sichuan-latest.parquet
    - language-v1/data/china-tianjin-latest.parquet
    - language-v1/data/china-tibet-latest.parquet
    - language-v1/data/china-xinjiang-latest.parquet
    - language-v1/data/china-yunnan-latest.parquet
    - language-v1/data/china-zhejiang-latest.parquet
    - language-v1/data/colombia-latest.parquet
    - language-v1/data/comores-latest.parquet
    - language-v1/data/congo-brazzaville-latest.parquet
    - language-v1/data/congo-democratic-republic-latest.parquet
    - language-v1/data/cook-islands-latest.parquet
    - language-v1/data/corse-latest.parquet
    - language-v1/data/costa-rica-latest.parquet
    - language-v1/data/croatia-latest.parquet
    - language-v1/data/cuba-latest.parquet
    - language-v1/data/cyprus-latest.parquet
    - language-v1/data/czech-republic-latest.parquet
    - language-v1/data/denmark-latest.parquet
    - language-v1/data/djibouti-latest.parquet
    - language-v1/data/east-timor-latest.parquet
    - language-v1/data/ecuador-latest.parquet
    - language-v1/data/egypt-latest.parquet
    - language-v1/data/el-salvador-latest.parquet
    - language-v1/data/england-latest.parquet
    - language-v1/data/equatorial-guinea-latest.parquet
    - language-v1/data/eritrea-latest.parquet
    - language-v1/data/estonia-latest.parquet
    - language-v1/data/ethiopia-latest.parquet
    - language-v1/data/extremadura-latest.parquet
    - language-v1/data/falklands-latest.parquet
    - language-v1/data/faroe-islands-latest.parquet
    - language-v1/data/fiji-latest.parquet
    - language-v1/data/finland-latest.parquet
    - language-v1/data/franche-comte-latest.parquet
    - language-v1/data/gabon-latest.parquet
    - language-v1/data/galicia-latest.parquet
    - language-v1/data/gcc-states-latest.parquet
    - language-v1/data/georgia-latest.parquet
    - language-v1/data/ghana-latest.parquet
    - language-v1/data/greece-latest.parquet
    - language-v1/data/greenland-latest.parquet
    - language-v1/data/guadeloupe-latest.parquet
    - language-v1/data/guatemala-latest.parquet
    - language-v1/data/guernsey-jersey-latest.parquet
    - language-v1/data/guinea-bissau-latest.parquet
    - language-v1/data/guinea-latest.parquet
    - language-v1/data/guyana-latest.parquet
    - language-v1/data/guyane-latest.parquet
    - language-v1/data/haiti-and-domrep-latest.parquet
    - language-v1/data/hamburg-latest.parquet
    - language-v1/data/haute-normandie-latest.parquet
    - language-v1/data/hessen-latest.parquet
    - language-v1/data/honduras-latest.parquet
    - language-v1/data/hungary-latest.parquet
    - language-v1/data/iceland-latest.parquet
    - language-v1/data/ile-de-clipperton-latest.parquet
    - language-v1/data/ile-de-france-latest.parquet
    - language-v1/data/india-central-zone-latest.parquet
    - language-v1/data/india-eastern-zone-latest.parquet
    - language-v1/data/india-north-eastern-zone-latest.parquet
    - language-v1/data/india-northern-zone-latest.parquet
    - language-v1/data/india-southern-zone-latest.parquet
    - language-v1/data/india-western-zone-latest.parquet
    - language-v1/data/indonesia-java-latest.parquet
    - language-v1/data/indonesia-kalimantan-latest.parquet
    - language-v1/data/indonesia-maluku-latest.parquet
    - language-v1/data/indonesia-nusa-tenggara-latest.parquet
    - language-v1/data/indonesia-papua-latest.parquet
    - language-v1/data/indonesia-sulawesi-latest.parquet
    - language-v1/data/indonesia-sumatra-latest.parquet
    - language-v1/data/iran-latest.parquet
    - language-v1/data/iraq-latest.parquet
    - language-v1/data/ireland-and-northern-ireland-latest.parquet
    - language-v1/data/islas-baleares-latest.parquet
    - language-v1/data/isle-of-man-latest.parquet
    - language-v1/data/israel-and-palestine-latest.parquet
    - language-v1/data/italy-latest.parquet
    - language-v1/data/ivory-coast-latest.parquet
    - language-v1/data/jamaica-latest.parquet
    - language-v1/data/japan-chubu-latest.parquet
    - language-v1/data/japan-chugoku-latest.parquet
    - language-v1/data/japan-hokkaido-latest.parquet
    - language-v1/data/japan-kansai-latest.parquet
    - language-v1/data/japan-kanto-latest.parquet
    - language-v1/data/japan-kyushu-latest.parquet
    - language-v1/data/japan-shikoku-latest.parquet
    - language-v1/data/japan-tohoku-latest.parquet
    - language-v1/data/jordan-latest.parquet
    - language-v1/data/kazakhstan-latest.parquet
    - language-v1/data/kenya-latest.parquet
    - language-v1/data/kiribati-latest.parquet
    - language-v1/data/kosovo-latest.parquet
    - language-v1/data/kyrgyzstan-latest.parquet
    - language-v1/data/la-rioja-latest.parquet
    - language-v1/data/languedoc-roussillon-latest.parquet
    - language-v1/data/laos-latest.parquet
    - language-v1/data/latvia-latest.parquet
    - language-v1/data/lebanon-latest.parquet
    - language-v1/data/lesotho-latest.parquet
    - language-v1/data/liberia-latest.parquet
    - language-v1/data/libya-latest.parquet
    - language-v1/data/liechtenstein-latest.parquet
    - language-v1/data/limousin-latest.parquet
    - language-v1/data/lithuania-latest.parquet
    - language-v1/data/lorraine-latest.parquet
    - language-v1/data/luxembourg-latest.parquet
    - language-v1/data/macedonia-latest.parquet
    - language-v1/data/madagascar-latest.parquet
    - language-v1/data/madrid-latest.parquet
    - language-v1/data/malawi-latest.parquet
    - language-v1/data/malaysia-singapore-brunei-latest.parquet
    - language-v1/data/maldives-latest.parquet
    - language-v1/data/mali-latest.parquet
    - language-v1/data/malta-latest.parquet
    - language-v1/data/marshall-islands-latest.parquet
    - language-v1/data/martinique-latest.parquet
    - language-v1/data/mauritania-latest.parquet
    - language-v1/data/mauritius-latest.parquet
    - language-v1/data/mayotte-latest.parquet
    - language-v1/data/mecklenburg-vorpommern-latest.parquet
    - language-v1/data/melilla-latest.parquet
    - language-v1/data/mexico-latest.parquet
    - language-v1/data/micronesia-latest.parquet
    - language-v1/data/midi-pyrenees-latest.parquet
    - language-v1/data/moldova-latest.parquet
    - language-v1/data/monaco-latest.parquet
    - language-v1/data/mongolia-latest.parquet
    - language-v1/data/montenegro-latest.parquet
    - language-v1/data/morocco-latest.parquet
    - language-v1/data/mozambique-latest.parquet
    - language-v1/data/murcia-latest.parquet
    - language-v1/data/myanmar-latest.parquet
    - language-v1/data/namibia-latest.parquet
    - language-v1/data/nauru-latest.parquet
    - language-v1/data/navarra-latest.parquet
    - language-v1/data/nepal-latest.parquet
    - language-v1/data/netherlands-latest.parquet
    - language-v1/data/new-caledonia-latest.parquet
    - language-v1/data/new-zealand-latest.parquet
    - language-v1/data/nicaragua-latest.parquet
    - language-v1/data/niedersachsen-latest.parquet
    - language-v1/data/niger-latest.parquet
    - language-v1/data/nigeria-latest.parquet
    - language-v1/data/niue-latest.parquet
    - language-v1/data/nord-est-latest.parquet
    - language-v1/data/nord-pas-de-calais-latest.parquet
    - language-v1/data/nordrhein-westfalen-latest.parquet
    - language-v1/data/north-korea-latest.parquet
    - language-v1/data/norway-latest.parquet
    - language-v1/data/pais-vasco-latest.parquet
    - language-v1/data/pakistan-latest.parquet
    - language-v1/data/palau-latest.parquet
    - language-v1/data/panama-latest.parquet
    - language-v1/data/papua-new-guinea-latest.parquet
    - language-v1/data/paraguay-latest.parquet
    - language-v1/data/pays-de-la-loire-latest.parquet
    - language-v1/data/peru-latest.parquet
    - language-v1/data/philippines-latest.parquet
    - language-v1/data/picardie-latest.parquet
    - language-v1/data/pitcairn-islands-latest.parquet
    - language-v1/data/poitou-charentes-latest.parquet
    - language-v1/data/poland-latest.parquet
    - language-v1/data/polynesie-francaise-latest.parquet
    - language-v1/data/portugal-latest.parquet
    - language-v1/data/provence-alpes-cote-d-azur-latest.parquet
    - language-v1/data/reunion-latest.parquet
    - language-v1/data/rheinland-pfalz-latest.parquet
    - language-v1/data/rhone-alpes-latest.parquet
    - language-v1/data/romania-latest.parquet
    - language-v1/data/russia-central-fed-district-latest.parquet
    - language-v1/data/russia-crimean-fed-district-latest.parquet
    - language-v1/data/russia-far-eastern-fed-district-latest.parquet
    - language-v1/data/russia-kaliningrad-latest.parquet
    - language-v1/data/russia-north-caucasus-fed-district-latest.parquet
    - language-v1/data/russia-northwestern-fed-district-latest.parquet
    - language-v1/data/russia-siberian-fed-district-latest.parquet
    - language-v1/data/russia-south-fed-district-latest.parquet
    - language-v1/data/russia-ural-fed-district-latest.parquet
    - language-v1/data/russia-volga-fed-district-latest.parquet
    - language-v1/data/rwanda-latest.parquet
    - language-v1/data/saarland-latest.parquet
    - language-v1/data/sachsen-anhalt-latest.parquet
    - language-v1/data/sachsen-latest.parquet
    - language-v1/data/saint-helena-ascension-and-tristan-da-cunha-latest.parquet
    - language-v1/data/samoa-latest.parquet
    - language-v1/data/sao-tome-and-principe-latest.parquet
    - language-v1/data/schleswig-holstein-latest.parquet
    - language-v1/data/scotland-latest.parquet
    - language-v1/data/senegal-and-gambia-latest.parquet
    - language-v1/data/serbia-latest.parquet
    - language-v1/data/seychelles-latest.parquet
    - language-v1/data/sierra-leone-latest.parquet
    - language-v1/data/slovakia-latest.parquet
    - language-v1/data/slovenia-latest.parquet
    - language-v1/data/solomon-islands-latest.parquet
    - language-v1/data/somalia-latest.parquet
    - language-v1/data/south-africa-latest.parquet
    - language-v1/data/south-korea-latest.parquet
    - language-v1/data/south-sudan-latest.parquet
    - language-v1/data/sri-lanka-latest.parquet
    - language-v1/data/sud-latest.parquet
    - language-v1/data/sudan-latest.parquet
    - language-v1/data/suriname-latest.parquet
    - language-v1/data/swaziland-latest.parquet
    - language-v1/data/sweden-latest.parquet
    - language-v1/data/switzerland-latest.parquet
    - language-v1/data/syria-latest.parquet
    - language-v1/data/taiwan-latest.parquet
    - language-v1/data/tajikistan-latest.parquet
    - language-v1/data/tanzania-latest.parquet
    - language-v1/data/thailand-latest.parquet
    - language-v1/data/thueringen-latest.parquet
    - language-v1/data/togo-latest.parquet
    - language-v1/data/tokelau-latest.parquet
    - language-v1/data/tonga-latest.parquet
    - language-v1/data/tunisia-latest.parquet
    - language-v1/data/turkey-latest.parquet
    - language-v1/data/turkmenistan-latest.parquet
    - language-v1/data/tuvalu-latest.parquet
    - language-v1/data/uganda-latest.parquet
    - language-v1/data/ukraine-latest.parquet
    - language-v1/data/uruguay-latest.parquet
    - language-v1/data/us-alabama-latest.parquet
    - language-v1/data/us-alaska-latest.parquet
    - language-v1/data/us-arizona-latest.parquet
    - language-v1/data/us-arkansas-latest.parquet
    - language-v1/data/us-california-latest.parquet
    - language-v1/data/us-colorado-latest.parquet
    - language-v1/data/us-connecticut-latest.parquet
    - language-v1/data/us-delaware-latest.parquet
    - language-v1/data/us-district-of-columbia-latest.parquet
    - language-v1/data/us-florida-latest.parquet
    - language-v1/data/us-georgia-latest.parquet
    - language-v1/data/us-hawaii-latest.parquet
    - language-v1/data/us-idaho-latest.parquet
    - language-v1/data/us-illinois-latest.parquet
    - language-v1/data/us-indiana-latest.parquet
    - language-v1/data/us-iowa-latest.parquet
    - language-v1/data/us-kansas-latest.parquet
    - language-v1/data/us-kentucky-latest.parquet
    - language-v1/data/us-louisiana-latest.parquet
    - language-v1/data/us-maine-latest.parquet
    - language-v1/data/us-maryland-latest.parquet
    - language-v1/data/us-massachusetts-latest.parquet
    - language-v1/data/us-michigan-latest.parquet
    - language-v1/data/us-minnesota-latest.parquet
    - language-v1/data/us-mississippi-latest.parquet
    - language-v1/data/us-missouri-latest.parquet
    - language-v1/data/us-montana-latest.parquet
    - language-v1/data/us-nebraska-latest.parquet
    - language-v1/data/us-nevada-latest.parquet
    - language-v1/data/us-new-hampshire-latest.parquet
    - language-v1/data/us-new-jersey-latest.parquet
    - language-v1/data/us-new-mexico-latest.parquet
    - language-v1/data/us-new-york-latest.parquet
    - language-v1/data/us-north-carolina-latest.parquet
    - language-v1/data/us-north-dakota-latest.parquet
    - language-v1/data/us-ohio-latest.parquet
    - language-v1/data/us-oklahoma-latest.parquet
    - language-v1/data/us-oregon-latest.parquet
    - language-v1/data/us-pennsylvania-latest.parquet
    - language-v1/data/us-puerto-rico-latest.parquet
    - language-v1/data/us-rhode-island-latest.parquet
    - language-v1/data/us-south-carolina-latest.parquet
    - language-v1/data/us-south-dakota-latest.parquet
    - language-v1/data/us-tennessee-latest.parquet
    - language-v1/data/us-texas-latest.parquet
    - language-v1/data/us-utah-latest.parquet
    - language-v1/data/us-vermont-latest.parquet
    - language-v1/data/us-virgin-islands-latest.parquet
    - language-v1/data/us-virginia-latest.parquet
    - language-v1/data/us-washington-latest.parquet
    - language-v1/data/us-west-virginia-latest.parquet
    - language-v1/data/us-wisconsin-latest.parquet
    - language-v1/data/us-wyoming-latest.parquet
    - language-v1/data/uzbekistan-latest.parquet
    - language-v1/data/valencia-latest.parquet
    - language-v1/data/vanuatu-latest.parquet
    - language-v1/data/venezuela-latest.parquet
    - language-v1/data/vietnam-latest.parquet
    - language-v1/data/wales-latest.parquet
    - language-v1/data/wallis-et-futuna-latest.parquet
    - language-v1/data/yemen-latest.parquet
    - language-v1/data/zambia-latest.parquet
    - language-v1/data/zimbabwe-latest.parquet
---

![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)

# OSM Polygon Description Tag

OpenStreetMap polygons with a non-empty `description` or
`description:<suffix>` tag, published as one GeoParquet file per regional PBF
extract. Every row retains the complete original tag map, full Polygon or
MultiPolygon geometry, WGS84 geodesic area, bounding box, and OSM provenance.

Source repository: [github.com/NoeFlandre/osm-polygon-description-tag](https://github.com/NoeFlandre/osm-polygon-description-tag).

Explore the pipeline metrics in the [Trackio dashboard](https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/?project=osm-polygon-description-tag&sidebar=hidden).

Read the [dataset presentation](https://noeflandre.github.io/osm-polygon-description-tag/slides/dataset/dataset.html) for a concise visual overview of the snapshot, methodology, and findings.

<!-- GENERATED:H3_MAP:START -->
![H3 density of description-tagged polygons](assets/description_polygon_density.png)
<!-- GENERATED:H3_MAP:END -->
Hexbin density of every described polygon at H3 resolution 3, drawn from each
row's geometry centroid on a logarithmic scale. Lighter cells contain more
polygons.
<!-- GENERATED:STATS:START -->
<!-- stats_sha256: 66fd9a37003bafab31afdd7c17a2412ed747424c56daf618560d1a63b5fc0464 -->
<!-- stats_schema_version: 6 -->
<!-- schema_version: 3 -->

## Dataset at a glance

| Metric | Value |
| --- | --- |
| Polygons | 906,631 |
| Parquet files | 386 |
| Download size | 719.8 MiB |
| Duplicate rows removed | 38,851 |
| Closed ways | 860,310 |
| Relations | 46,321 |
| Polygon geometries | 0 |
| MultiPolygon geometries | 906,631 |

## Description coverage

| Description type | Values | Total words | Median words per description |
| --- | ---: | ---: | ---: |
| Base descriptions | 887,077 | 5,109,668 | 4 |
| Localized descriptions | 32,049 | 213,972 | 3 |

### Most common localized suffixes

These are exact OSM tag suffixes and are not validated language codes.

| Suffix | Description values |
| --- | ---: |
| `de` | 8,668 |
| `en` | 6,679 |
| `it` | 3,385 |
| `fr` | 1,560 |
| `ru` | 1,474 |
| `pl` | 1,168 |
| `zh` | 789 |
| `es` | 787 |
| `ar` | 501 |
| `nl` | 451 |

### Area distribution

![Area distribution of description-tagged polygons](assets/area_distribution.png)

Area buckets span <1 m² to >=100B m² on a logarithmic scale; each bar shows the number of polygons in that bucket (total 906,631).

**OSM object timestamps (UTC):** 2007-08-03T10:18:25 to 2026-07-25T21:38:32

Detailed machine-readable statistics, exact suffix frequencies, rejection counts, and per-file SHA-256 provenance are available in [`stats.json`](stats.json).
<!-- GENERATED:STATS:END -->

## Terminology

- **Closed way**: an OSM way whose first and last nodes share an identifier
  and that `osmium export` emits as an area when its tags mark it as a
  polygon feature.
- **Relation**: an OSM object (here `type=multipolygon` or `type=boundary`)
  grouping several ways into one logical feature; kept when it assembles into
  a valid polygon.
- **Polygon**: a single outer-ring area geometry.
- **MultiPolygon**: a geometry of one or more disjoint Polygon parts,
  produced for assembled multipolygon and boundary relations.
- **Base description**: the exact text of the `description=*` tag on a feature.
- **Localized description**: the exact text of a suffixed
  `description:<suffix>=*` tag; the suffix is preserved verbatim and is not
  validated as a language code.

## What is included

- Tagged closed ways that OSM classifies as areas, excluding `area=no`.
- Successfully assembled `type=multipolygon` and `type=boundary` relations.
- Exact base and localized descriptions and names.
- Complete original OSM tags, full WKB geometry, `area_m2`, and bounding boxes.

Nodes, open ways, undescribed features, and failed polygon assemblies are not
included. Cross-region duplicates are removed globally before publication.

## Schema

- **Identity:** `source_pbf`, `osm_type`, `osm_id`, `osm_url`
- **OSM provenance:** `version`, `changeset`, `timestamp`
- **Convenience text fields:** `name`, `localized_names`, `description`,
  `localized_descriptions`
- **Authoritative source tags:** `tags`
- **Spatial fields:** `geometry_type`, `area_m2`, `bbox_min_x`, `bbox_min_y`,
  `bbox_max_x`, `bbox_max_y`, `geometry`

`geometry` is WKB with GeoParquet 1.1 metadata and OGC:CRS84 longitude/latitude
semantics. The `tags` key/value list is authoritative; convenience text fields
are exact derived views.

## Load the data

```python
import pyarrow.parquet as pq

table = pq.read_table("data/<region>-latest.parquet")
```

```python
import geopandas as gpd

gdf = gpd.read_parquet("data/<region>-latest.parquet")
```

## Methodology

`osmium export` applies standard OSM area handling and emits polygon geometry
only. The pipeline retains features with at least one exact non-empty
description tag, computes geodesic WGS84 area with holes and multipolygon
components included, validates GeoParquet and manifest identities, and writes
artifacts atomically.

All displayed statistics are generated from validated Parquet files and their
matching manifests. No counts are handwritten.

<!-- GENERATED:LANGUAGE_V1:START -->
## Language annotations (`language-v1`)

This optional configuration adds a language label to each description value when
the detector meets its confidence policy, plus sentence splits. It has one row
per *description value*—not per polygon. The default configuration and its files
are unchanged.

| Measure | Value |
| --- | ---: |
| Annotations | 919126 |
| Detected | 572328 |
| Uncertain | 340924 |
| Non-linguistic | 5874 |
| Distinct languages | 324 |
| Split into sentences | 534731 |
| Sentences | 649186 |

**Models and provenance.** The primary detector is Lingua 2.2.0. The run used pipeline `lingua+glotlid-v3-fallback` on input snapshot `9a03d00020191df375c25d3e4fa9b79e28ac9f8d83868d274a508ab954a84e98` with detector configuration `5d87faafca790cf96bbd495caca50bd8dfe4bf2cf50be8c19ff65989e2581980`. When Lingua is uncertain, the pinned GlotLID v3 model (`cis-lmu/glotlid`, revision `85cd6716494360367b75f642b5bc78667605d0b4`, SHA-256 `a818b6bd42a628ab47d3dfc1578c7ea615c45381f3494c42535e31e8c4cafc9e`) is used as a fallback.

**How to read this.** `language_code` is null for uncertain or non-linguistic
values. `top_score`, `runner_up_score`, and `margin` are **raw detector scores,
not calibrated probabilities**; they must not be read as confidence percentages.
No accuracy has been measured because this dataset has no ground-truth labels.
Short or mixed-language text may remain unresolved (`mixed_text`), and text
outside the detector's supported set may be misclassified as a supported
language. A `description:<suffix>` key is opaque and is not used as a language
label. Text with no letters is `non_linguistic`.
<!-- GENERATED:LANGUAGE_V1:END -->

## Limitations

- Suffixes such as `en` or `pt-BR` are preserved exactly but are not validated
  as language codes.
- Text comes directly from OpenStreetMap and may vary in quality, language,
  formatting, and completeness.
- Cross-region overlaps are globally deduplicated by `(osm_type, osm_id)` before publication.
- Geometry and tags reflect the source extracts at their recorded OSM
  timestamps.

## License and attribution

Derived data is © OpenStreetMap contributors and available under the
[Open Database License](https://opendatacommons.org/licenses/odbl/) (ODbL).
Users and redistributors must comply with its attribution and share-alike
requirements. Pipeline code is Apache-2.0.

## Reproducibility

The public source repository contains the versioned extraction policy,
deterministic reporting code, validation contracts, and the stoppable,
resumable `just run-and-publish` workflow.

## Citation

If you use this software or its dataset, please cite the repository using the
metadata in [`CITATION.cff`](https://github.com/NoeFlandre/osm-polygon-description-tag/blob/main/CITATION.cff).
GitHub provides formatted citation downloads through **Cite this repository**.
