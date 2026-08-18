import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function section(start, end) {
  const from = source.indexOf(start)
  assert.notEqual(from, -1, `missing section start: ${start}`)
  const to = source.indexOf(end, from)
  assert.notEqual(to, -1, `missing section end: ${end}`)
  return source.slice(from, to)
}

test('durable Groups is part of the bundled hermes-bots plugin, not a second desktop plugin id', () => {
  assert.match(source, /const ID = 'hermes-bots'/)
  assert.match(source, /const GROUPS_KEY = \[ID, 'groups'\]/)
  assert.match(source, /const GROUP_ROUTE = '\/bot-groups'/)
  assert.match(source, /id: 'groups-page'/)
  assert.match(source, /id: 'groups-nav'/)
  assert.match(source, /render: \(\) => jsx\(DurableGroupRoomPage/)
})

test('durable Groups keeps stable bot-instance identities and excludes remote thin rows', () => {
  const block = section('function reconcileBotIdentities', 'function newGroupMutationKey')
  assert.match(block, /roster\.filter\(bot => !bot\.remoteSource\)/)
  assert.match(block, /\/bots\/reconcile/)
  assert.match(block, /instanceId/)
  assert.match(block, /void saveBotMeta\(assignment\.profile_name/)
})

test('durable Groups supports managed identity, leader, icons, membership, and durable messages', () => {
  assert.match(source, /const GROUP_COLORS = \[/)
  assert.match(source, /const GROUP_EMOJIS = \[/)
  assert.match(source, /function DurableCreateGroupDialog/)
  assert.match(source, /function DurableGroupSettingsDialog/)
  assert.match(source, /leader_bot_instance_id/)
  assert.match(source, /\/membership/)
  assert.match(source, /groupMessagesKey/)
  assert.match(source, /appendGroupTranscriptMessage/)
})

test('durable Groups restores room-scoped model selection and slash completion', () => {
  assert.match(source, /group-chat-prefs/)
  assert.match(source, /host\.request\('model\.options'/)
  assert.match(source, /function DurableGroupModelPicker/)
  assert.match(source, /host\.request\('commands\.catalog'\)/)
  assert.match(source, /host\.request\('complete\.slash'/)
  assert.match(source, /function DurableGroupSlashMenu/)
})

test('persistent Groups never prewarm members and Bot rows wake on click, not hover', () => {
  const durable = section('// ── persistent managed Groups', '// ── roster pane')
  assert.doesNotMatch(durable, /warmGroupMembers/)
  assert.doesNotMatch(durable, /warmProfile|warmAgent/)

  const botRow = section('function BotRow(', '// ── model picker')
  assert.doesNotMatch(botRow, /onPointerEnter:\s*warm/)
  assert.doesNotMatch(botRow, /host\.warmProfile|host\.warmAgent/)
  assert.match(botRow, /onClick:\s*open/)
})

test('quick upstream Group Chat remains present alongside persistent managed Groups', () => {
  assert.match(source, /const GROUP_CHAT_MAX_ROUNDS = 3/)
  assert.match(source, /const GROUP_CHAT_MAX_MESSAGES = 10/)
  assert.match(source, /function GroupChatWorkspace/)
  assert.match(source, /function runGroupChatRounds/)
  assert.match(source, /requestForBot\(member, 'session\.create'/)
})

test('route registration is optional for stripped-down SDK test harnesses', () => {
  assert.match(source, /if \(typeof ROUTES_AREA !== 'undefined'\)/)
  assert.match(source, /if \(typeof SIDEBAR_NAV_AREA !== 'undefined'\)/)
})
