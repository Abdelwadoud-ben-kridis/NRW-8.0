// Every user-visible string lives here. Nobody hard-codes text in the HTML.
// Jury switches language -> change ONE line:  export const L = FR;  (or EN)

export const EN = {
  lang: "EN",
  title: "SCW / NRW 8.0",
  subtitle: "SMART CORE WAREHOUSE",
  clock: "Simulated clock", speed: "Speed", jump6: "+6 h", reset: "Reset",
  scenario: "Load demo scenario", more: "More", dbExplorer: "DB Explorer",
  newRef: "New reference", refCode: "Code", refLabel: "Label",
  unitMass: "Unit mass (g)", color: "Colour",
  addRef: "Add reference", cancelRef: "Cancel",
  refAdded: (ref) => `Reference ${ref} added`,
  plant: "Plant model — core arrival",
  barcodeSection: "Barcode registration (worker action)",
  barcodeNotice: "A worker labels a box and registers its per-noyau weight "
                + "BEFORE it ever reaches the conveyor — the scanner below "
                + "just reads this back, it never counts anything itself.",
  bcId: "Barcode ID", bcMass: "g / noyau (blank = reference default)",
  bcRegister: "Register barcode", bcPick: "Scan a registered barcode",
  bcAuto: "— auto (issue a new one) —",
  bcNeedsId: "Barcode ID is required",
  article: "Reference", qty: "Quantity", anomaly: "Inject anomaly",
  arrive: "Box arrives on conveyor", quickBox: "Quick box, no ESP32",
  env: "Curing room climate (monitoring)", temp: "Temperature", hum: "Humidity",
  cureNow: "Cure requirement (fixed)",
  demand: "Production demand", ask: "Request cores",
  confirm: "Confirm pick", cancel: "Cancel",
  proposal: "Automatic proposal (FIFO)",
  picks: "Boxes selected", rejected: "Boxes rejected — and why",
  noOrder: "No order yet. Ask production for cores.",
  shortfall: "Shortfall", allocated: "Allocated", requested: "Requested",
  demandAvail: (n) => `${n} in stock`,
  demandTooMuch: (n) => `Only ${n} in stock for this reference — cannot request more.`,
  warehouse: "Warehouse",
  pendingOrders: "Pending reservations", lockExpiresIn: (s) => `expires in ${s} s`,
  batchesTitle: "Production batches", noBatches: "No open production batches.",
  batchOpened: "opened as a production batch — see below",
  batchReady: "ready to ship", batchCuring: "curing",
  batchEmpty: "no boxes produced yet", batchProduce: "Produce a box",
  lockExpired: "expired",
  kpiSlots: "Slots used", kpiReady: "Ready boxes", kpiDrying: "Curing",
  kpiQuar: "Quarantine", kpiCores: "Cores available", kpiFree: "Free slots",
  inventory: "Inventory", box: "Box", ref: "Ref", state: "State",
  code: "Code",
  slot: "Slot", cure: "Cure", left: "Left", conf: "Confidence",
  counts: "Beam / Weight", stored: "Stored at", age: "Age",
  byRef: "Stock by reference", fifoHead: "next out (FIFO)", none: "none ready",
  nextOut: "Next out (FIFO)", colReady: "Ready", colDrying: "Curing",
  filterAll: "All references", kpiReserved: "Reserved",
  events: "Live event log", device: "ESP32", broker: "Broker",
  online: "online", offline: "offline", mode: "Mode",
  legend: "Slot colour", cores: "cores", boxes: "boxes",
  refusedShort: "refused",

  // --- top bar / system status --------------------------------------------
  systemOnline: "SYSTEM ONLINE", systemReconnecting: "RECONNECTING",
  systemOffline: "OFFLINE", systemPolling: "POLLING FALLBACK",
  contract: "CONTRACT", shortcuts: "Shortcuts", simControls: "Sim controls",
  dbPass: "PASS", dbWarn: "WARN", dbFail: "FAIL", dbChecks: "CHECKS",

  // --- live operation panel -------------------------------------------------
  liveOperation: "Live operation", stageIdle: "SYSTEM NOMINAL",
  stageReceiving: "RECEIVING", stageCounting: "COUNTING",
  stageStabilizing: "STABILIZING", stageStoring: "STORING",
  stageAllocating: "ALLOCATING", stageReserved: "RESERVED",
  stageConfirmed: "CONFIRMED", stageQuarantine: "QUARANTINE",
  stageFault: "FAULT", stageCuring: "CURING IN PROGRESS", stageReady: "READY",
  stageIdleSub: "no activity in progress",
  stageSub: "derived from ESP32 state, crane command and last order",

  // --- ESP32 instrument panel -----------------------------------------------
  liveEsp32: "Live ESP32", beamCount: "cores (beam)", visionRef: "vision id",
  grossMass: "gross mass",
  stable: "stable", unstable: "settling", lastSeen: "last seen",
  fw: "firmware", noSignalYet: "no telemetry yet",
  esp32St: { IDLE: "IDLE", COUNTING: "COUNTING", STABILIZING: "STABILIZING",
             DONE: "DONE", FAULT: "FAULT" },

  // --- crane -----------------------------------------------------------------
  stackerCrane: "Stacker crane", craneIdle: "IDLE", craneStore: "STORE",
  cranePick: "PICK", craneNoCmd: "no active command",

  // --- curing preview ----------------------------------------------------
  curingModule: "Curing", curingNone: "no boxes curing",
  curingMore: (n) => `+${n} more curing`,

  // --- quarantine ----------------------------------------------------------
  quarantineModule: "Quarantine", quarantineNone: "no boxes in quarantine",
  unknownRef: "UNKNOWN REFERENCE", netMass: "Net mass", perCore: "Per core",
  gap: "Gap", recount: "Re-weigh", archive: "Archive",

  // --- rack / slot inspector -----------------------------------------------
  rackView: "RACK", twinView: "3D TWIN", rackTitle: "Warehouse rack — 306 slots",
  slotFree: "free slot", slotSelectHint: "click a slot to inspect it",
  reference: "Reference", label2: "Label", quantity: "Quantity",
  available: "Available", arrival: "Arrival", lock: "Lock", reason2: "Reason",
  noLock: "none", noReason: "—",

  // --- inventory table -------------------------------------------------------
  searchPlaceholder: "search box / ref / label…",

  // --- reservation panel ------------------------------------------------------
  orderExpired: "ORDER EXPIRED", reservationReleased: "RESERVATION RELEASED",
  noPending: "No pending reservations.",

  // --- database health -------------------------------------------------------
  database: "Database",
  st: {
    INCOMING: "Incoming", IDENTIFYING: "Identifying", COUNTING: "Counting",
    STORING: "Storing", DRYING: "Drying", READY: "Ready", RESERVED: "Reserved",
    PICKING: "Picking", EMPTY: "Empty", ARCHIVED: "Archived",
    QUARANTINE: "Quarantine",
  },
  // event-log kinds, from backend/warehouse.py and backend/main.py's event()
  ev: {
    box_in: "stored", quarantine: "quarantine", cured: "cured",
    demand: "demand", pick_done: "picked", order_cancel: "cancelled",
    order_expired: "reservation expired", clock_jump: "clock",
    clock_speed: "speed", env: "climate", article_new: "new reference",
    box_done_ignored: "duplicate ignored", box_done_invalid: "invalid message",
    arrival_fallback: "L1 fallback", system_reset: "reset",
    scenario_loaded: "scenario loaded",
    batch_opened: "batch opened", batch_shipped: "batch shipped",
    batch_cancelled: "batch cancelled", barcode_registered: "barcode registered",
    box_archived: "archived", box_recount: "re-weighed",
  },
  // The decision engine authors its rejection vocabulary in French (it is
  // P2's territory and algo/test_engine.py asserts on the exact strings), so
  // the UI translates it here on the way to the screen. Anything unmatched
  // falls through unchanged.
  rej: {
    "en cours de reception": "inbound",
    "sechage insuffisant": "insufficient cure",
    "reserve": "reserved",
    "prelevement en cours": "being picked",
    "vide": "empty",
    "archive": "archived",
    "quarantaine": "quarantine",
    "indisponible": "unavailable",
    "plus recent (FIFO)": "newer (FIFO)",
    "stock insuffisant": "not enough stock",
    "reserve au lot": "held for a production batch",
  },
  det: {
    "pas encore stocke": "not stored yet",
    "identification en cours": "identifying",
    "comptage en cours": "counting",
    "transfert vers le rack": "moving to the rack",
    "controle de coherence": "coherence check",
    "autre commande": "another order",
    "0 noyau": "0 cores",
    "besoin deja couvert par des box plus anciens":
      "demand already covered by older boxes",
  },
  detReadyIn: (h) => `ready in ${h} h`,
  detMismatch: (net, code, unit, gap) =>
    `net mass ${net} g doesn't match barcode ${code} (${unit} g/core expected, gap ${gap} g)`,
  detUnknownBarcode: (id) => `unregistered barcode: ${id}`,
  detReusedBarcode: (id) => `barcode already used by ${id}`,
  detVisionMismatch: (seen, code, declared) =>
    `vision sees reference ${seen}, but barcode ${code} declares ${declared}`,
  detCountGap: (weight, vision) =>
    `count disagreement: scale says ${weight}, vision sees ${vision} cores`,
  partialPick: "partial", partialPickHint: "remainder stays in stock, same age",
  etaNone: "no estimate — nothing left curing for this reference",
  etaHint: (clockLabel) => `enough will be ready by ${clockLabel}`,
};

export const FR = {
  lang: "FR",
  title: "SCW / NRW 8.0",
  subtitle: "SMART CORE WAREHOUSE",
  clock: "Horloge simulée", speed: "Vitesse", jump6: "+6 h", reset: "Réinitialiser",
  scenario: "Charger le scénario démo", more: "Plus", dbExplorer: "Base de données",
  newRef: "Nouvelle référence", refCode: "Code", refLabel: "Libellé",
  unitMass: "Masse unitaire (g)", color: "Couleur",
  addRef: "Ajouter la référence", cancelRef: "Annuler",
  refAdded: (ref) => `Référence ${ref} ajoutée`,
  plant: "Modèle physique",
  barcodeSection: "Enregistrement du code-barre (action ouvrier)",
  barcodeNotice: "Un ouvrier étiquette la caisse et enregistre son poids "
                + "par noyau AVANT qu'elle n'atteigne le convoyeur — le "
                + "scanner ci-dessous ne fait que le relire, il ne compte "
                + "jamais rien lui-même.",
  bcId: "Code-barre", bcMass: "g / noyau (vide = valeur de la référence)",
  bcRegister: "Enregistrer le code-barre", bcPick: "Scanner un code-barre enregistré",
  bcAuto: "— auto (en émettre un) —",
  bcNeedsId: "Le code-barre est obligatoire",
  article: "Référence", qty: "Quantité", anomaly: "Injecter une anomalie",
  arrive: "Le box arrive sur le convoyeur", quickBox: "Box rapide, sans ESP32",
  env: "Climat de la zone de séchage (suivi)", temp: "Température", hum: "Humidité",
  cureNow: "Séchage requis (fixe)",
  demand: "Demande de production", ask: "Demander des noyaux",
  confirm: "Valider", cancel: "Annuler",
  proposal: "Proposition automatique (FIFO)",
  picks: "Box retenus", rejected: "Box écartés — et pourquoi",
  noOrder: "Aucune commande. Exprimez un besoin de production.",
  shortfall: "Manquant", allocated: "Alloué", requested: "Demandé",
  demandAvail: (n) => `${n} en stock`,
  demandTooMuch: (n) => `Seulement ${n} en stock pour cette référence — impossible de demander plus.`,
  warehouse: "Entrepôt",
  pendingOrders: "Réservations en attente",
  batchesTitle: "Lots de production", noBatches: "Aucun lot de production en cours.",
  batchOpened: "ouvert comme lot de production — voir ci-dessous",
  batchReady: "prêt à expédier", batchCuring: "en séchage",
  batchEmpty: "aucune caisse produite pour l'instant", batchProduce: "Produire une caisse",
  lockExpiresIn: (s) => `expire dans ${s} s`, lockExpired: "expirée",
  kpiSlots: "Emplacements", kpiReady: "Box prêts", kpiDrying: "En séchage",
  kpiQuar: "Quarantaine", kpiCores: "Noyaux disponibles", kpiFree: "Emplacements libres",
  inventory: "Inventaire", box: "Box", ref: "Réf", state: "État",
  code: "Code",
  slot: "Emplacement", cure: "Séchage", left: "Reste", conf: "Confiance",
  counts: "Barrière / Pesée", stored: "Stocké à", age: "Âge",
  byRef: "Stock par référence", fifoHead: "prochain sorti (FIFO)", none: "aucun prêt",
  nextOut: "Prochain sorti (FIFO)", colReady: "Prêts", colDrying: "Séchage",
  filterAll: "Toutes les références", kpiReserved: "Réservé",
  events: "Journal d'activité", device: "ESP32", broker: "Broker",
  online: "en ligne", offline: "hors ligne", mode: "Mode",
  legend: "Couleurs", cores: "noyaux", boxes: "box",
  refusedShort: "écartés",

  // --- top bar / system status --------------------------------------------
  systemOnline: "SYSTÈME EN LIGNE", systemReconnecting: "RECONNEXION",
  systemOffline: "HORS LIGNE", systemPolling: "REPLI POLLING",
  contract: "CONTRAT", shortcuts: "Raccourcis", simControls: "Simulation",
  dbPass: "OK", dbWarn: "AVERT.", dbFail: "ÉCHEC", dbChecks: "CONTRÔLES",

  // --- live operation panel -------------------------------------------------
  liveOperation: "Opération en cours", stageIdle: "SYSTÈME NOMINAL",
  stageReceiving: "RÉCEPTION", stageCounting: "COMPTAGE",
  stageStabilizing: "STABILISATION", stageStoring: "RANGEMENT",
  stageAllocating: "ALLOCATION", stageReserved: "RÉSERVÉ",
  stageConfirmed: "VALIDÉ", stageQuarantine: "QUARANTAINE",
  stageFault: "DÉFAUT", stageCuring: "SÉCHAGE EN COURS", stageReady: "PRÊT",
  stageIdleSub: "aucune activité en cours",
  stageSub: "déduit de l'état ESP32, de la commande du pont et de la commande en cours",

  // --- ESP32 instrument panel -----------------------------------------------
  liveEsp32: "ESP32 en direct", beamCount: "noyaux (barrière)", visionRef: "identification vision",
  grossMass: "masse brute",
  stable: "stable", unstable: "stabilisation", lastSeen: "vu pour la dernière fois",
  fw: "firmware", noSignalYet: "pas encore de télémétrie",
  esp32St: { IDLE: "INACTIF", COUNTING: "COMPTAGE", STABILIZING: "STABILISATION",
             DONE: "TERMINÉ", FAULT: "DÉFAUT" },

  // --- crane -----------------------------------------------------------------
  stackerCrane: "Pont transstockeur", craneIdle: "INACTIF", craneStore: "RANGER",
  cranePick: "PRÉLEVER", craneNoCmd: "aucune commande active",

  // --- curing preview ----------------------------------------------------
  curingModule: "Séchage", curingNone: "aucun box en séchage",
  curingMore: (n) => `+${n} autres en séchage`,

  // --- quarantine ----------------------------------------------------------
  quarantineModule: "Quarantaine", quarantineNone: "aucun box en quarantaine",
  unknownRef: "RÉFÉRENCE INCONNUE", netMass: "Masse nette", perCore: "Par noyau",
  gap: "Écart", recount: "Repeser", archive: "Archiver",

  // --- rack / slot inspector -----------------------------------------------
  rackView: "RACK", twinView: "JUMEAU 3D", rackTitle: "Rack de l'entrepôt — 306 emplacements",
  slotFree: "emplacement libre", slotSelectHint: "cliquez un emplacement pour l'inspecter",
  reference: "Référence", label2: "Libellé", quantity: "Quantité",
  available: "Disponible", arrival: "Arrivée", lock: "Verrou", reason2: "Motif",
  noLock: "aucun", noReason: "—",

  // --- inventory table -------------------------------------------------------
  searchPlaceholder: "rechercher box / réf / libellé…",

  // --- reservation panel ------------------------------------------------------
  orderExpired: "COMMANDE EXPIRÉE", reservationReleased: "RÉSERVATION LIBÉRÉE",
  noPending: "Aucune réservation en attente.",

  // --- database health -------------------------------------------------------
  database: "Base de données",
  st: {
    INCOMING: "Arrivée", IDENTIFYING: "Identification", COUNTING: "Comptage",
    STORING: "Rangement", DRYING: "Séchage", READY: "Prêt", RESERVED: "Réservé",
    PICKING: "Prélèvement", EMPTY: "Vide", ARCHIVED: "Archivé",
    QUARANTINE: "Quarantaine",
  },
  ev: {
    box_in: "stocké", quarantine: "quarantaine", cured: "séché",
    demand: "demande", pick_done: "prélevé", order_cancel: "annulée",
    order_expired: "réservation expirée", clock_jump: "horloge",
    clock_speed: "vitesse", env: "climat", article_new: "nouvelle référence",
    box_done_ignored: "doublon ignoré", box_done_invalid: "message invalide",
    arrival_fallback: "repli L1", system_reset: "réinitialisation",
    scenario_loaded: "scénario chargé",
    batch_opened: "lot ouvert", batch_shipped: "lot expédié",
    batch_cancelled: "lot annulé", barcode_registered: "code-barre enregistré",
    box_archived: "archivé", box_recount: "repesé",
  },
  // In FR the engine's own wording is already correct — this map only
  // restores the accents it cannot carry through MQTT/SQLite safely.
  rej: {
    "en cours de reception": "en cours de réception",
    "sechage insuffisant": "séchage insuffisant",
    "reserve": "réservé",
    "prelevement en cours": "prélèvement en cours",
    "vide": "vide",
    "archive": "archivé",
    "quarantaine": "quarantaine",
    "indisponible": "indisponible",
    "plus recent (FIFO)": "plus récent (FIFO)",
    "stock insuffisant": "stock insuffisant",
    "reserve au lot": "réservé à un lot de production",
  },
  det: {
    "pas encore stocke": "pas encore stocké",
    "identification en cours": "identification en cours",
    "comptage en cours": "comptage en cours",
    "transfert vers le rack": "transfert vers le rack",
    "controle de coherence": "contrôle de cohérence",
    "autre commande": "autre commande",
    "0 noyau": "0 noyau",
    "besoin deja couvert par des box plus anciens":
      "besoin déjà couvert par des box plus anciens",
  },
  detReadyIn: (h) => `prêt dans ${h} h`,
  detMismatch: (net, code, unit, gap) =>
    `masse nette ${net} g incompatible avec le code-barre ${code} (${unit} g/noyau attendu, écart ${gap} g)`,
  detUnknownBarcode: (id) => `code-barre non enregistré : ${id}`,
  detReusedBarcode: (id) => `code-barre déjà utilisé par ${id}`,
  detVisionMismatch: (seen, code, declared) =>
    `la vision détecte la référence ${seen}, mais le code-barre ${code} annonce ${declared}`,
  detCountGap: (weight, vision) =>
    `écart de comptage : pesée ${weight}, vision ${vision} noyaux`,
  partialPick: "partiel", partialPickHint: "le reste reste en stock, même ancienneté",
  etaNone: "aucune estimation — plus rien en séchage pour cette référence",
  etaHint: (clockLabel) => `assez sera prêt à ${clockLabel}`,
};

// Runtime language switch (dashboard/app.js's language toggle button), kept
// as ES module live bindings: `export let L` means every module that does
// `import { L } from "./labels.js"` sees the SAME reassignment the instant
// setLang() runs here -- no per-component duplication of the FR/EN choice.
export const LANGS = { EN, FR };

function _initialLang() {
  try {
    const saved = localStorage.getItem("scw_lang");
    if (saved && LANGS[saved]) return saved;
  } catch { /* private mode / disabled storage: fall back below */ }
  return "EN";
}

// <<< previously "the one line to flip for the jury" — now also a live
// toggle in the top bar; this default is just the initial paint. >>>
export let L = LANGS[_initialLang()];

export function setLang(key) {
  if (!LANGS[key] || LANGS[key] === L) return false;
  L = LANGS[key];
  try { localStorage.setItem("scw_lang", key); } catch { /* ignore */ }
  return true;
}
