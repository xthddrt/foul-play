#!/usr/bin/env node
// RANDOM-ACTION VALIDATION CORPUS generator (v7/random100k).
//
// This is gen_corpus.js (the rounds-1..10 conformance-corpus generator, the
// checker's home provenance) with exactly ONE behavioral delta and one label:
//
//   * ACTION POLICY is a flag. --policy uniform (default) draws ONE uniform
//     sample over ALL legal choices (non-disabled moves + legal switches),
//     instead of the stock class split (switch 15% / move 85%). Uniform tilts
//     switch-heavy (~5/9 early), reaching hazard/switch-in/trapping states the
//     0.85-move corpora rarely visit. --policy stock keeps the class split.
//     Both draw terastallize at --tera-prob (default 0.15) after picking a
//     move, while still able -- same tera exposure as every prior corpus.
//   * Game tag is battle-gen9randombattle-synthu<N> ("u" = uniform family) so
//     files match the established `battle-gen9randombattle-synth*` globs while
//     never colliding with a prior corpus name.
//
// Everything else -- seed derivation (same golden-ratio hash, same role
// constants; disjointness from every prior corpus is BY INDEX RANGE, use
// --start 5000001+), output format (single p1-perspective `<tag>_synthopp.log`
// + `<tag>_synthopp.teams.json` sidecar with full sets incl. evs/ivs/actual
// stats and both sides' first |request|), rqid injection, |error|-chunk
// dropping, turn cap, timeout -- is byte-identical in mechanism to
// gen_corpus.js. The active-decision branch is simplified to the singles/
// tera-only reality of gen9randombattle (no Z/dynamax/mega paths, which are
// unreachable in this format).
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PS = process.env.PS_DIR || '/Users/sallyliu/pokemon-fast-bot/pokemon-showdown';
const { BattleStream, getPlayerStreams } = require(path.join(PS, 'dist/sim/battle-stream'));
const { Teams } = require(path.join(PS, 'dist/sim/teams'));
const { RandomPlayerAI } = require(path.join(PS, 'dist/sim/tools/random-player-ai'));
const { Dex } = require(path.join(PS, 'dist/sim/dex'));

let count = 0, start = 0, out = '', quiet = false, policy = 'uniform';
let teraProb = 0.15, expectSetsSha = '';
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === '--count') count = parseInt(argv[++i]);
  else if (argv[i] === '--start') start = parseInt(argv[++i]);
  else if (argv[i] === '--out') out = argv[++i];
  else if (argv[i] === '--policy') policy = argv[++i];
  else if (argv[i] === '--tera-prob') teraProb = parseFloat(argv[++i]);
  else if (argv[i] === '--expect-sets-sha') expectSetsSha = argv[++i];
  else if (argv[i] === '--quiet') quiet = true;
}
if (!count || !start || !out || !['uniform', 'stock'].includes(policy)) {
  console.error('usage: gen_random_corpus.js --count N --start IDX --out DIR [--policy uniform|stock] [--tera-prob P] [--expect-sets-sha SHA1] [--quiet]');
  process.exit(1);
}
fs.mkdirSync(out, { recursive: true });

// PS BUILD IDENTITY — carried in every sidecar so the corpus itself proves
// which randbats data generated it (round 1's provenance anomaly: an unknown
// newer PS build produced 5 level deltas vs the pinned checkout).  With
// --expect-sets-sha the generator REFUSES to run on any other build.
const PS_IDENTITY = (() => {
  const setsSha = crypto.createHash('sha1')
    .update(fs.readFileSync(path.join(PS, 'data/random-battles/gen9/sets.json')))
    .digest('hex');
  let version = '';
  try { version = JSON.parse(fs.readFileSync(path.join(PS, 'package.json'))).version; } catch (e) {}
  let gitHead = '';
  try {
    const head = fs.readFileSync(path.join(PS, '.git/HEAD'), 'utf8').trim();
    gitHead = head.startsWith('ref: ')
      ? fs.readFileSync(path.join(PS, '.git', head.slice(5)), 'utf8').trim()
      : head;
  } catch (e) { /* tarball-shipped checkout without .git */ }
  return { sets_json_sha1: setsSha, ps_version: version, git_head: gitHead };
})();
if (expectSetsSha && PS_IDENTITY.sets_json_sha1 !== expectSetsSha) {
  console.error(`FATAL: PS build mismatch: sets.json sha1 ${PS_IDENTITY.sets_json_sha1}, expected ${expectSetsSha} (PS_DIR=${PS})`);
  process.exit(3);
}

const GOLD = 0x9E3779B9;
const ROLE = { battle: 0xA11CE, p1_ai: 0xB0B1, p2_ai: 0xC4C2, p1_team: 0xD1D3, p2_team: 0xE2E4 };
const seedFor = (n, c) =>
  'sodium,' + (((Math.imul(GOLD, n) + c) >>> 0).toString(16).padStart(8, '0')) +
  ((n >>> 0).toString(16).padStart(8, '0'));

const MOVE_PROB = 0.85, TURN_CAP = 500, GAME_TIMEOUT_MS = 120000;

function range(start, end, step = 1) {
  if (end === undefined) { end = start; start = 0; }
  const result = [];
  for (; start <= end; start += step) result.push(start);
  return result;
}

class RecordingAI extends RandomPlayerAI {
  constructor(stream, options, onChunk) {
    super(stream, options);
    this.onChunk = onChunk;
    this.rqid = 0;
  }
  receive(chunk) {
    // Server-style rqid injection: every request wave reaches both sides, so a
    // per-side counter equals the room's shared wave counter.
    if (chunk.startsWith('|request|')) {
      this.rqid++;
      chunk = chunk.replace(/^\|request\|(.*)$/m,
        (_, json) => `|request|${json.slice(0, -1)},"rqid":${this.rqid}}`);
    }
    this.onChunk(chunk);
    super.receive(chunk);
  }
  receiveRequest(request) {
    if (request.wait) {
      // wait request - do nothing
    } else if (request.forceSwitch) {
      // identical to gen_corpus.js (revival-aware, uniform among legal)
      const pokemon = request.side.pokemon;
      const chosen = [];
      const choices = request.forceSwitch.map((mustSwitch, i) => {
        if (!mustSwitch) return `pass`;
        const canSwitch = range(1, 6).filter(j => (
          pokemon[j - 1] &&
          (j > request.forceSwitch.length || pokemon[i].reviving) &&
          !chosen.includes(j) &&
          !pokemon[j - 1].condition.endsWith(` fnt`) === !pokemon[i].reviving
        ));
        if (!canSwitch.length) return `pass`;
        const target = this.chooseSwitch(
          undefined,
          canSwitch.map(slot => ({ slot, pokemon: pokemon[slot - 1] }))
        );
        chosen.push(target);
        return `switch ${target}`;
      });
      this.choose(choices.join(`, `));
    } else if (request.teamPreview) {
      this.choose(this.chooseTeamPreview(request.side.pokemon));
    } else if (request.active) {
      // gen9randombattle is singles: one active slot, tera the only gimmick.
      const pokemon = request.side.pokemon;
      const active = request.active[0];
      if (pokemon[0].condition.endsWith(` fnt`) || pokemon[0].commanding) {
        this.choose(`pass`);
        return;
      }
      const moves = range(1, active.moves.length)
        .filter(j => !active.moves[j - 1].disabled)
        .map(j => `move ${j}`);
      const canSwitch = range(1, 6).filter(j => (
        pokemon[j - 1] &&
        !pokemon[j - 1].active &&
        !pokemon[j - 1].condition.endsWith(` fnt`)
      ));
      const switches = active.trapped ? [] : canSwitch.map(j => `switch ${j}`);

      let choice;
      if (policy === 'uniform') {
        // ONE uniform draw over every legal choice.
        const pool = moves.concat(switches);
        if (!pool.length) throw new Error(`no legal choice`);
        choice = pool[this.prng.random(pool.length)];
      } else {
        // stock: switch at 1-MOVE_PROB when possible, uniform within class.
        if (switches.length && (!moves.length || this.prng.random() > MOVE_PROB)) {
          choice = switches[this.prng.random(switches.length)];
        } else {
          choice = moves[this.prng.random(moves.length)];
        }
      }
      if (choice.startsWith(`move `) && active.canTerastallize &&
          this.prng.random() < teraProb) {
        choice += ` terastallize`;
      }
      this.choose(choice);
    }
  }
}

function parseDetails(details) {
  const parts = details.split(', ');
  let gender = 'N', shiny = false;
  for (const p of parts.slice(1)) {
    if (p === 'M' || p === 'F') gender = p;
    if (p === 'shiny') shiny = true;
  }
  return { gender, shiny };
}

function teamEntry(set, req, speciesByIdent) {
  const { gender, shiny } = parseDetails(req.details);
  const maxhp = parseInt(req.condition.split('/')[1]);
  return {
    // THE IMMUTABLE SET SPECIES, never the live one.  The sidecar contract
    // (fp/replay/damage_membership.py `_fold_forme_duplicate_rows` docstring)
    // is "the sidecar's own species, which sim/pokemon.ts never moves": the
    // checker keys one row per PS roster slot and recomputes a battle forme's
    // stats from that forme's base stats itself.  Writing the LIVE species
    // (`speciesByIdent`, a battle-start snapshot) made an Imposter Ditto that
    // transformed at switch-in collide with a real teammate inside one forme
    // family -- synthu6256926 recorded p2's Ditto as `Terapagos-Terastal` and
    // the real Terapagos as `Terapagos`, so the terastallized Terapagos joined
    // the Ditto row and derived its flat 133 stats (two false damage findings).
    species: Dex.species.get(set.species || set.name).name,
    level: set.level,
    gender,
    ability: set.ability,
    item: set.item,
    moves: set.moves.map(m => Dex.toID(m)),
    evs: set.evs,
    ivs: set.ivs,
    teraType: req.teraType,
    shiny,
    stats: {
      hp: maxhp,
      atk: req.stats.atk, def: req.stats.def,
      spa: req.stats.spa, spd: req.stats.spd, spe: req.stats.spe,
    },
  };
}

async function runGame(n) {
  const tag = `battle-gen9randombattle-synthu${n}`;
  const logPath = path.join(out, `${tag}_synthopp.log`);
  const teamsPath = path.join(out, `${tag}_synthopp.teams.json`);
  const hptruthPath = path.join(out, `${tag}_synthopp.hptruth.json`);
  if (fs.existsSync(logPath) && fs.existsSync(teamsPath) && fs.existsSync(hptruthPath)) return 'skip';

  const t1 = Teams.generate('gen9randombattle', { seed: seedFor(n, ROLE.p1_team) });
  const t2 = Teams.generate('gen9randombattle', { seed: seedFor(n, ROLE.p2_team) });

  const battleStream = new BattleStream();
  const streams = getPlayerStreams(battleStream);

  const st = {
    chunks: [], dropped: 0, p1First: null, p2First: null,
    turns: 0, winner: '', capped: false, timedOut: false, ended: false,
    hptruth: {}, hptruthMismatches: 0,
  };

  // HP-TRUTH observation (side-effect-free: reads the battle object, draws no
  // PRNG, writes nothing into any stream).  Snapshot at the moment p1's chunk
  // callback processes `|turn|N`: the battle is parked awaiting BOTH sides'
  // turn-N decisions right then (p1's own choice happens after this callback,
  // inside super.receive), so `battle.pX.pokemon[*].hp` IS the exact pre-turn
  // HP the checker reconstructs for turn N.
  //
  // KEYS ARE THE ORIGINAL SET NAME (`p.name` -- nickname == set species, and a
  // random team never duplicates species, so keys are UNIQUE PER SIDE by
  // construction).  The first cut keyed by speciesByIdent (the transformed
  // species snapshot, matching the teams.json convention) and COLLIDED when a
  // lead Impostor Ditto transformed into a species its own team also carries:
  // exam game synthu6078304 -- p2's Ditto became "Scrafty", overwrote the real
  // Scrafty's entry with the dead Ditto's [0, 225], and the checker dutifully
  // pinned a live Scrafty to 0 HP (false hard finding).  `p.name` is stable
  // across Transform / Illusion / mid-game forme changes.
  const snapHpTruth = (turnNum) => {
    const b = battleStream.battle;
    if (!b) return;
    if (b.turn !== turnNum) { st.hptruthMismatches++; return; }
    const entry = { p1: {}, p2: {} };
    for (const sideId of ['p1', 'p2']) {
      for (const p of b[sideId].pokemon) {
        if (entry[sideId][p.name] !== undefined) st.hptruthKeyCollisions = (st.hptruthKeyCollisions || 0) + 1;
        entry[sideId][p.name] = [p.hp, p.maxhp];
      }
    }
    st.hptruth[turnNum] = entry;
  };

  const grabFirstRequest = (chunk, key) => {
    if (st[key]) return;
    for (const line of chunk.split('\n')) {
      if (line.startsWith('|request|')) {
        const req = JSON.parse(line.slice('|request|'.length));
        if (req.side) {
          st[key] = req.side;
          // Snapshot species at battle start (leads already switched in:
          // Shields Down, Impostor, Crowned formes resolved; bench untouched).
          if (!st.speciesByIdent) {
            st.speciesByIdent = {};
            for (const side of [battleStream.battle.p1, battleStream.battle.p2]) {
              for (const p of side.pokemon) st.speciesByIdent[p.fullname] = p.species.name;
            }
          }
          return;
        }
      }
    }
  };

  const finish = () => {
    if (st.ended) return;
    st.ended = true;
    try { streams.omniscient.writeEnd(); } catch (e) { /* already ended */ }
  };

  const p1 = new RecordingAI(streams.p1,
    { seed: seedFor(n, ROLE.p1_ai) },
    chunk => {
      if (!chunk.startsWith('|')) return;
      if (chunk.startsWith('|error|')) { st.dropped++; return; }
      st.chunks.push(chunk);
      grabFirstRequest(chunk, 'p1First');
      for (const line of chunk.split('\n')) {
        if (line.startsWith('|turn|')) {
          st.turns = parseInt(line.slice(6));
          snapHpTruth(st.turns);
          if (st.turns >= TURN_CAP && !st.capped) {
            st.capped = true;
            void streams.omniscient.write('>forcetie');
          }
        } else if (line.startsWith('|win|')) {
          st.winner = line.slice(5);
          setImmediate(finish);
        } else if (line === '|tie') {
          setImmediate(finish);
        }
      }
    });
  const p2 = new RecordingAI(streams.p2,
    { seed: seedFor(n, ROLE.p2_ai) },
    chunk => grabFirstRequest(chunk, 'p2First'));

  const done = Promise.all([p1.start(), p2.start()]);

  await streams.omniscient.write(
    `>start {"formatid":"gen9randombattle","seed":"${seedFor(n, ROLE.battle)}"}\n` +
    `>player p1 {"name":"synthbot","team":${JSON.stringify(Teams.pack(t1))}}\n` +
    `>player p2 {"name":"synthopp","team":${JSON.stringify(Teams.pack(t2))}}`
  );

  await Promise.race([
    done,
    new Promise(res => setTimeout(() => { st.timedOut = true; finish(); res(); }, GAME_TIMEOUT_MS).unref()),
  ]);

  const log = st.chunks
    .map(c => `DEBUG    Received message from websocket: >${tag}\n${c}`)
    .join('\n') + '\n';
  fs.writeFileSync(logPath, log);

  const speciesByIdent = st.speciesByIdent || {};

  const sidecar = {
    meta: {
      game: n,
      format: 'gen9randombattle',
      battle_tag: tag,
      seed: seedFor(n, ROLE.battle),
      p1_name: 'synthbot',
      p2_name: 'synthopp',
      p1_ai_seed: seedFor(n, ROLE.p1_ai),
      p2_ai_seed: seedFor(n, ROLE.p2_ai),
      p1_team_seed: seedFor(n, ROLE.p1_team),
      p2_team_seed: seedFor(n, ROLE.p2_team),
      policy,
      tera_prob: teraProb,
      turns: st.turns,
      winner: st.winner,
      capped: st.capped,
      error_chunks_dropped: st.dropped,
      timed_out: st.timedOut,
      generated_at: new Date().toISOString(),
      ps_identity: PS_IDENTITY,
    },
    teams: {
      p1: { name: 'synthbot', id: 'p1', team: t1.map((s, i) => teamEntry(s, st.p1First.pokemon[i], speciesByIdent)) },
      p2: { name: 'synthopp', id: 'p2', team: t2.map((s, i) => teamEntry(s, st.p2First.pokemon[i], speciesByIdent)) },
    },
    p1_first_request_side: st.p1First,
    p2_first_request_side: st.p2First,
  };
  fs.writeFileSync(teamsPath, JSON.stringify(sidecar, null, 1));
  fs.writeFileSync(hptruthPath, JSON.stringify({
    meta: {
      game: n,
      battle_tag: tag,
      turn_mismatches: st.hptruthMismatches,
      key_collisions: st.hptruthKeyCollisions || 0,
      turns_recorded: Object.keys(st.hptruth).length,
    },
    turns: st.hptruth,
  }, null, 1));
  return 'ok';
}

(async () => {
  let ok = 0, skip = 0, fail = 0;
  const t0 = Date.now();
  for (let n = start; n < start + count; n++) {
    try {
      const r = await runGame(n);
      if (r === 'skip') skip++; else ok++;
      if (!quiet && (ok + skip) % 100 === 0) console.log(`progress ${ok + skip}/${count}`);
    } catch (err) {
      fail++;
      console.error(`GAME ${n} FAILED: ${err.message}`);
    }
  }
  const el = (Date.now() - t0) / 1000;
  console.log(`done: ${ok} generated, ${skip} skipped, ${fail} failed, ${el.toFixed(1)}s (${(ok / Math.max(el, 1e-9)).toFixed(2)} games/s)`);
  if (fail) process.exit(2);
})();
