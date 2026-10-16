# Planification et contrats entre modules

Mise à jour par le coordinateur à chaque phase. État : ✅ fait et testé, 🔄 en cours, ⏳ à faire.

## 1. Organisation des agents

Environnement vérifié : Claude Code fournit des sous-agents (outil `Agent`). Les agents ne partagent pas de fichiers : chaque fichier a **un seul propriétaire** à la fois. Le coordinateur écrit les contrats avant le travail en parallèle et assure seul l'intégration.

| Agent | Rôle | Fichiers dont il est propriétaire |
|---|---|---|
| 1. Coordinateur et architecte | Architecture, contrats, services, intégration, ROADMAP et PLANNING | `config.py`, `core/document_models.py`, `core/coordinates.py`, `core/errors.py`, `core/image_loader.py`, `services/*`, `tests/synthetic.py`, `tests/conftest.py`, `tests/test_models.py`, docs |
| 2. Auditeur | Ancien projet en lecture seule | `AUDIT.md` (réalisé par le coordinateur avant la répartition) |
| 3. Vision et prétraitement | Géométrie, réglages photométriques, corrections locales, modes A, B et C | `core/image_adjustments.py`, `core/image_preprocessing.py`, `tests/test_preprocessing.py`, `tests/test_adjustments.py` |
| 4. OCR et mise en page | Tesseract, orientation, blocs, colonnes, ordre, styles, illustrations | `core/ocr_engine.py`, `core/layout_analysis.py`, `core/illustration_extraction.py`, `tests/test_ocr.py`, `tests/test_layout.py`, `tests/test_illustrations.py` |
| 5. Reconstruction PDF | Polices, rendu A et B, aperçu, validation | `core/fonts.py`, `core/pdf_generator.py`, `core/pdf_validator.py`, `tests/test_pdf_generation.py` |
| 6. Interface Streamlit | Écrans, éditeur d'images, éditeur de blocs (composant SVG) | `app.py`, `ui/*`, `.streamlit/config.toml`, `tests/test_ui_smoke.py` |
| 7. Qualité | Tests d'intégration, rapport | `tests/test_integration.py`, `tests/test_document_roundtrip.py`, `TEST_REPORT.md` |

Ordre : contrats (1) → vision, OCR et PDF **en parallèle** (3, 4, 5) → services d'intégration (1) → interface (6) → qualité (7) → parcours réel et documentation finale (1).

## 2. Contrats communs (déjà écrits, ne pas modifier sans le coordinateur)

- `core/document_models.py` : `Project`, `Page`, `TextBlock`, `ImageBlock`, `Box`, `ImageAdjustments`, `LocalAdjustment`, `GeometrySettings`, `RenderSettings`, `ProcessingSettings`.
- `core/coordinates.py` : repère normalisé 0..1 du **cadre de page** (origine en haut à gauche) ; conversion en pixels et en points PDF (origine en bas à gauche).
- `core/errors.py` : exceptions avec message utilisateur en français.
- Images en mémoire : `numpy.ndarray` **RGB uint8 (H, W, 3)** ou **gris uint8 (H, W)**. Pas d'objet PIL dans les interfaces de calcul (sauf mention).
- `tests/synthetic.py` : pages de test avec vérité terrain.

## 3. Interfaces à implémenter

### Agent 3 : `core/image_adjustments.py`

```python
def apply_adjustments(rgb: np.ndarray, adj: ImageAdjustments) -> np.ndarray
    # neutre (ou adj.enabled False) -> tableau identique (np.array_equal). Même forme, uint8.
    # grayscale/binarization -> renvoie quand même du RGB (3 canaux identiques).
def apply_local_adjustments(rgb: np.ndarray, locals: list[LocalAdjustment], target: AdjustmentTarget,
                            reference: np.ndarray | None = None) -> np.ndarray
    # seuls les pixels des rectangles (target == cible ou BOTH, enabled) changent ;
    # restore_original -> pixels copiés depuis `reference` (le cadre géométrique sans correction).
def estimate_white_balance_gains(rgb, mode: WhiteBalance) -> tuple[float, float, float]
def histogram(rgb) -> dict[str, np.ndarray]     # pour l'interface (canaux R, G, B, L ; 256 classes)
```

### Agent 3 : `core/image_preprocessing.py`

```python
@dataclass FrameResult: image: np.ndarray (RGB); geometry: GeometrySettings (detected_* remplis); warnings: list[str]
def load_oriented(path: Path, source_index: int = 0) -> np.ndarray   # délègue à core.image_loader.read_image
def detect_page_quad(rgb) -> list[tuple[float, float]] | None        # 4 coins tl, tr, br, bl en 0..1, None si pas de feuille nette
def estimate_skew(rgb_or_gray, max_angle: float = 10.0) -> float      # degrés, sens antihoraire, 0.0 si incertain
def build_frame(original_rgb, geometry: GeometrySettings, detect: bool = True,
                orientation_detector: Callable[[np.ndarray], int | None] | None = None) -> FrameResult
    # étapes et ordre définis dans GeometrySettings ; detect=False réutilise detected_* (reproductible)
def render_print_image(frame_rgb, page_adjust: ImageAdjustments, locals: list[LocalAdjustment],
                       mode: ProcessingMode, text_boxes: list[Box] = ()) -> np.ndarray
    # mode B (couleur) : réglages globaux puis locaux, JAMAIS de binarisation implicite
    # mode C : régions de texte nettoyées (fond blanchi), illustrations en traitement couleur
def render_ocr_image(frame_rgb, ocr_adjust: ImageAdjustments, locals: list[LocalAdjustment],
                     mode: ProcessingMode, illustration_boxes: list[Box] = ()) -> np.ndarray   # gris uint8
    # mode A et C : niveaux de gris, correction d'éclairage ; les régions illustrées sont blanchies en mode C
def suggest_mode(frame_rgb) -> ProcessingMode   # TEXT, ILLUSTRATION ou MIXED (jamais AUTO)
def binarize_sauvola(gray) -> np.ndarray
def downscale(rgb, max_side: int) -> tuple[np.ndarray, float]   # (image, facteur)
```

### Agent 4 : `core/ocr_engine.py`

```python
@dataclass OCRWordPx: text, left, top, width, height, conf (float, -1 inconnu), block, par, line, word
@dataclass OCRResult: words: list[OCRWordPx]; width: int; height: int; languages: list[str]; preparation: str
def tesseract_version() -> str | None
def available_languages() -> list[str]                    # sans "osd"
def check_languages(langs: list[str]) -> None              # lève OCRUnavailableError (message explicite)
def run_ocr(gray: np.ndarray, languages: list[str], psm: int = 3, timeout: int | None = None) -> OCRResult
def read_page(gray, languages, retry_binarized: bool = True, low_confidence: float = 60) -> OCRResult
def detect_orientation(rgb) -> tuple[int, float] | None   # (rotation horaire à appliquer, confiance) via OSD
def ocr_region(gray, box: Box, languages) -> tuple[str, float]
```

### Agent 4 : `core/illustration_extraction.py`

```python
@dataclass DetectedRegion: box: Box; kind: str ("photo", "drawing", "table"); confidence: float; uncertain: bool; reason: str
def detect_illustrations(frame_rgb, text_boxes_px: list[tuple[int, int, int, int]] = ()) -> list[DetectedRegion]
def crop_region(frame_rgb, box: Box, padding: float = 0.0) -> np.ndarray   # sans perte, pleine résolution
```

### Agent 4 : `core/layout_analysis.py`

```python
@dataclass LayoutResult: blocks: list[TextBlock | ImageBlock-sans-fichier]; columns: int; margins: Margins;
                         printed_page_number: str; warnings: list[str]; mean_confidence: float
def analyse_layout(ocr: OCRResult, frame_rgb: np.ndarray, regions: list[DetectedRegion],
                   page_height_pt: float, settings: ProcessingSettings) -> LayoutResult
    # TextBlock : type, texte (= ocr_text), lignes, confiance, needs_review, styles estimés
    #   (font_size en points pour une page de hauteur page_height_pt, gras, italique si possible, couleur, alignement, interligne)
    # régions -> ImageBlock avec image_path="" (le service découpe et enregistre)
    # ordre de lecture : bandes pleine largeur, puis colonnes de gauche à droite ; en-têtes d'abord, pieds en dernier
def reading_order(blocks) -> int   # réordonne en place, renvoie le nombre de colonnes
```

### Agent 5 : `core/fonts.py`, `core/pdf_generator.py`, `core/pdf_validator.py`

```python
fonts.available_families() -> dict[str, FontFamily]   # familles TTF trouvées (FONT_DIRS) + génériques "Serif", "Sans", "Mono"
fonts.resolve(family, bold, italic) -> str             # nom ReportLab enregistré ; repli explicite
fonts.missing_characters(font_name, text) -> set[str]

ImageProvider = Callable[[Page, ImageBlock], PIL.Image.Image]   # illustration corrigée et validée (fournie par le service)
PageImageProvider = Callable[[Page], PIL.Image.Image]          # image de page imprimable (mode A)
@dataclass PageReport: page_index, page_id, warnings: list[str], overflows: list[str], overlaps: int,
                       reconstructed: bool, text_blocks: int, image_blocks: int
@dataclass RenderReport: pages: list[PageReport]; warnings (propriété agrégée)
def generate_pdf(project: Project, output: Path | BinaryIO, mode: Literal["reconstruction", "facsimile"],
                 image_provider: ImageProvider, page_image_provider: PageImageProvider | None = None,
                 page_ids: list[str] | None = None) -> RenderReport
def render_page_png(project, page, image_provider, mode="reconstruction", width_px=900,
                    page_image_provider=None) -> tuple[PIL.Image.Image, PageReport]   # aperçu = PDF rasterisé (pypdfium2)
pdf_validator.validate_pdf(path, project, mode, page_ids=None) -> ValidationReport (dict sérialisable)
    # nombre de pages, tailles, texte extractible par page, similarité texte attendu et lu, images trouvées et attendues
```

Règles du mode B : page blanche ; blocs texte en vrai texte (polices TTF intégrées) ; `hidden` exclus ; une page sans bloc n'est **jamais** remplacée par la photo : page blanche et `reconstructed=False` signalé. Débordement : `RenderSettings.overflow_policy`. Tailles de police multipliées par `coordinates.font_scale`.

## 4. Versions d'une page (non destructif)

| # | Version | Où | Régénérable depuis |
|---|---|---|---|
| 1 | Original | `originals/<page>.<ext>` | jamais modifié |
| 2 | Cadre (recadré, redressé) | `work/<page>_frame.png` | 1 et `GeometrySettings` |
| 3 | Image OCR | `work/<page>_ocr.png` | 2, `ocr_adjustments` et réglages locaux |
| 4 | Blocs et OCR | `project.json` | 3 (relance OCR) |
| 5 | Illustration extraite | `illustrations/<block>_src.png` (PNG) | 2 et la boîte |
| 6 | Illustration corrigée | **paramètres** `ImageBlock.adjustments` appliqués à 5 à la volée | 5 |
| 7 | Page reconstruite | aperçu = PDF rasterisé | 4 et 6 |
| 8 | PDF final | `exports/*.pdf` | 7 |

Une illustration est rendue ainsi : source (5), puis `page.print_adjustments` (réglages couleur de la page), puis les corrections locales qui la recouvrent, puis `crop`, puis `ImageBlock.adjustments`. Les réglages photométriques ne déplacent aucun bloc. Une modification de `GeometrySettings` après l'OCR rend `page.ocr_outdated` vrai : l'interface le signale et propose la relance. Les quarts de tour sont reportés exactement sur les blocs (`rotate_box_quarter`).

## 5. Suivi

| Phase | État | Validation |
|---|---|---|
| 1. Audit et contrats | ✅ | `AUDIT.md` ; `tests/test_models.py` 10/10 |
| 2. Import et stockage | 🔄 | tests services |
| 3. Prétraitement et éditeur d'image (cœur) | 🔄 agent 3 | tests neutres, local, couleur, géométrie |
| 4 à 6. OCR, mise en page, illustrations | 🔄 agent 4 | tests sur pages synthétiques |
| 7. Persistance | 🔄 | aller-retour JSON, rechargement sans OCR |
| 8 et 10. PDF et validation | 🔄 agent 5 | texte extractible, images, tailles |
| 9. Interface | 🔄 | point d'entrée, éditeurs de blocs et export ajoutés ; test de fumée `AppTest` |
| 11. Qualité et parcours réel | ⏳ | `TEST_REPORT.md` |
