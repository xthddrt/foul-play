#!/usr/bin/env node
// Synthetic gen9randombattle corpus generator (reconstruction of the lost
// pokemon-ai/tools/gen_corpus.js, validated byte-for-byte against S3 corpora
// modulo |t:| wall-clock lines and teams.json generated_at).
//
// Per game N, five PRNG seeds derive from the golden-ratio hash
//   high32 = (0x9E3779B9 * N + C) mod 2^32, low32 = N
// with role constants C: battle=0xA11CE, p1_ai=0xB0B1, p2_ai=0xC4C2,
// p1_team=0xD1D3, p2_team=0xE2E4. Both players are stock RandomPlayerAI
// (move 0.85, "mega"→terastallize 0.15). The log is player-1's websocket view
// prefixed like a foul-play log; the sidecar carries full teams + each side's
// first request for membership replay.
'use strict';
const fs = require('fs');
const path = require('path');

const PS = process.env.PS_DIR || '/Users/sallyliu/pokemon-fast-bot/pokemon-showdown';
const { BattleStream, getPlayerStreams } = require(path.join(PS, 'dist/sim/battle-stream'));
const { Teams } = require(path.join(PS, 'dist/sim/teams'));
const { RandomPlayerAI } = require(path.join(PS, 'dist/sim/tools/random-player-ai'));
const { Dex } = require(path.join(PS, 'dist/sim/dex'));

let count = 0, start = 0, out = '', quiet = false;
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === '--count') count = parseInt(argv[++i]);
  else if (argv[i] === '--start') start = parseInt(argv[++i]);
  else if (argv[i] === '--out') out = argv[++i];
  else if (argv[i] === '--quiet') quiet = true;
}
if (!count || !start || !out) {
  console.error('usage: gen_corpus.js --count N --start IDX --out DIR [--quiet]');
  process.exit(1);
}
fs.mkdirSync(out, { recursive: true });

const GOLD = 0x9E3779B9;
const ROLE = { battle: 0xA11CE, p1_ai: 0xB0B1, p2_ai: 0xC4C2, p1_team: 0xD1D3, p2_team: 0xE2E4 };
const seedFor = (n, c) =>
  'sodium,' + (((Math.imul(GOLD, n) + c) >>> 0).toString(16).padStart(8, '0')) +
  ((n >>> 0).toString(16).padStart(8, '0'));

const MOVE_PROB = 0.85, TERA_PROB = 0.15, TURN_CAP = 500, GAME_TIMEOUT_MS = 120000;

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
  // Stock RandomPlayerAI.receiveRequest with ONE change: gen9 terastallization
  // participates in the `change` draw (`|| canTerastallize`), matching the
  // original generator's PRNG call sequence and 0.15 tera rate.
  receiveRequest(request) {
    if (request.wait) {
      // wait request - do nothing
    } else if (request.forceSwitch) {
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
      let [canMegaEvo, canUltraBurst, canZMove, canDynamax, canTerastallize] = [true, true, true, true, true];
      const pokemon = request.side.pokemon;
      const chosen = [];
      const choices = request.active.map((active, i) => {
        if (pokemon[i].condition.endsWith(` fnt`) || pokemon[i].commanding) return `pass`;
        canMegaEvo = canMegaEvo && active.canMegaEvo;
        canUltraBurst = canUltraBurst && active.canUltraBurst;
        canZMove = canZMove && !!active.canZMove;
        canDynamax = canDynamax && !!active.canDynamax;
        canTerastallize = canTerastallize && !!active.canTerastallize;
        const change = (canMegaEvo || canUltraBurst || canDynamax) && this.prng.random() < this.mega;
        const useMaxMoves = (!active.canDynamax && active.maxMoves) || (change && canDynamax);
        const possibleMoves = useMaxMoves ? active.maxMoves.maxMoves : active.moves;
        let canMove = range(1, possibleMoves.length).filter(j => (
          !possibleMoves[j - 1].disabled
        )).map(j => ({
          slot: j,
          move: possibleMoves[j - 1].move,
          target: possibleMoves[j - 1].target,
          zMove: false,
        }));
        if (canZMove) {
          canMove.push(...range(1, active.canZMove.length)
            .filter(j => active.canZMove[j - 1])
            .map(j => ({
              slot: j,
              move: active.canZMove[j - 1].move,
              target: active.canZMove[j - 1].target,
              zMove: true,
            })));
        }
        const hasAlly = pokemon.length > 1 && !pokemon[i ^ 1].condition.endsWith(` fnt`);
        const filtered = canMove.filter(m => m.target !== `adjacentAlly` || hasAlly);
        canMove = filtered.length ? filtered : canMove;
        const moves = canMove.map(m => {
          let move = `move ${m.slot}`;
          if (request.active.length > 1) {
            if ([`normal`, `any`, `adjacentFoe`].includes(m.target)) {
              move += ` ${1 + this.prng.random(2)}`;
            }
            if (m.target === `adjacentAlly`) {
              move += ` -${(i ^ 1) + 1}`;
            }
            if (m.target === `adjacentAllyOrSelf`) {
              if (hasAlly) {
                move += ` -${1 + this.prng.random(2)}`;
              } else {
                move += ` -${i + 1}`;
              }
            }
          }
          if (m.zMove) move += ` zmove`;
          return { choice: move, move: m };
        });
        const canSwitch = range(1, 6).filter(j => (
          pokemon[j - 1] &&
          !pokemon[j - 1].active &&
          !chosen.includes(j) &&
          !pokemon[j - 1].condition.endsWith(` fnt`)
        ));
        const switches = active.trapped ? [] : canSwitch;
        if (switches.length && (!moves.length || this.prng.random() > this.move)) {
          const target = this.chooseSwitch(
            active,
            canSwitch.map(slot => ({ slot, pokemon: pokemon[slot - 1] }))
          );
          chosen.push(target);
          return `switch ${target}`;
        } else if (moves.length) {
          const move = this.chooseMove(active, moves);
          if (move.endsWith(` zmove`)) {
            canZMove = false;
            return move;
          } else if (change) {
            if (canTerastallize) {
              canTerastallize = false;
              return `${move} terastallize`;
            } else if (canDynamax) {
              canDynamax = false;
              return `${move} dynamax`;
            } else if (canMegaEvo) {
              canMegaEvo = false;
              return `${move} mega`;
            } else {
              canUltraBurst = false;
              return `${move} ultra`;
            }
          } else {
            // Original generator's tera rule: after sampling a move, draw once
            // and terastallize at tera_prob while still able.
            if (canTerastallize && this.prng.random() < this.mega) {
              canTerastallize = false;
              return `${move} terastallize`;
            }
            return move;
          }
        } else {
          throw new Error(`${this.constructor.name} unable to make choice ${i}.`);
        }
      });
      this.choose(choices.join(`, `));
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
  const tag = `battle-gen9randombattle-synth${n}`;
  const logPath = path.join(out, `${tag}_synthopp.log`);
  const teamsPath = path.join(out, `${tag}_synthopp.teams.json`);
  if (fs.existsSync(logPath) && fs.existsSync(teamsPath)) return 'skip';

  const t1 = Teams.generate('gen9randombattle', { seed: seedFor(n, ROLE.p1_team) });
  const t2 = Teams.generate('gen9randombattle', { seed: seedFor(n, ROLE.p2_team) });

  const battleStream = new BattleStream();
  const streams = getPlayerStreams(battleStream);

  const st = {
    chunks: [], dropped: 0, p1First: null, p2First: null,
    turns: 0, winner: '', capped: false, timedOut: false, ended: false,
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
    { move: MOVE_PROB, mega: TERA_PROB, seed: seedFor(n, ROLE.p1_ai) },
    chunk => {
      if (!chunk.startsWith('|')) return;
      if (chunk.startsWith('|error|')) { st.dropped++; return; }
      st.chunks.push(chunk);
      grabFirstRequest(chunk, 'p1First');
      for (const line of chunk.split('\n')) {
        if (line.startsWith('|turn|')) {
          st.turns = parseInt(line.slice(6));
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
    { move: MOVE_PROB, mega: TERA_PROB, seed: seedFor(n, ROLE.p2_ai) },
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
      move_prob: MOVE_PROB,
      tera_prob: TERA_PROB,
      turns: st.turns,
      winner: st.winner,
      capped: st.capped,
      error_chunks_dropped: st.dropped,
      timed_out: st.timedOut,
      generated_at: new Date().toISOString(),
    },
    teams: {
      p1: { name: 'synthbot', id: 'p1', team: t1.map((s, i) => teamEntry(s, st.p1First.pokemon[i], speciesByIdent)) },
      p2: { name: 'synthopp', id: 'p2', team: t2.map((s, i) => teamEntry(s, st.p2First.pokemon[i], speciesByIdent)) },
    },
    p1_first_request_side: st.p1First,
    p2_first_request_side: st.p2First,
  };
  fs.writeFileSync(teamsPath, JSON.stringify(sidecar, null, 1));
  return 'ok';
}

(async () => {
  let ok = 0, skip = 0, fail = 0;
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
  console.log(`done: ${ok} generated, ${skip} skipped, ${fail} failed`);
  if (fail) process.exit(2);
})();
