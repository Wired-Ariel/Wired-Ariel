-- Derivato da lua-websockets (https://github.com/lipp/lua-websockets)
-- Copyright (c) 2012 by Gerhard Lipp <gelipp@gmail.com> - licenza MIT (testo completo nel
-- file COPYRIGHT di lua-websockets). Preso via Celio-mGBA-Link (GPL-3.0). Vedi THIRD_PARTY_NOTICES.md.

local base64 = require'websocket.base64'
local sha1 = require'websocket.sha1'

local mrandom = math.random

local read_n_bytes = function(str, pos, n)
  pos = pos or 1
  return pos+n, string.byte(str, pos, pos + n - 1)
end

local read_int8 = function(str, pos)
  return read_n_bytes(str, pos, 1)
end

local read_int16 = function(str, pos)
  local new_pos,a,b = read_n_bytes(str, pos, 2)
  return new_pos, (a << 8) + b
end

local read_int32 = function(str, pos)
  local new_pos,a,b,c,d = read_n_bytes(str, pos, 4)
  return new_pos,
  (a << 24) +
  (b << 16) +
  (c << 8 ) +
  d
end

local pack_bytes = string.char

local write_int8 = pack_bytes

local write_int16 = function(v)
  return pack_bytes((v >> 8), (v & 0xFF))
end

local write_int32 = function(v)
  return pack_bytes(
    ((v >> 24) & 0xFF),
    ((v >> 16) & 0xFF),
    ((v >>  8) & 0xFF),
    (v & 0xFF)
  )
end

-- used for generate key random ops
math.randomseed(os.time())

local sha1_crypto = function(msg)
  return sha1.sha1_binary(msg)
end


local base64_encode = function(data)
  return (base64.encode(data))
end

local DEFAULT_PORTS = {ws = 80, wss = 443}

local parse_url = function(url)
  local protocol, address, uri = url:match('^(%w+)://([^/]+)(.*)$')
  if not protocol then error('Invalid URL:'..url) end
  protocol = protocol:lower()
  local host, port = address:match("^(.+):(%d+)$")
  if not host then
    host = address
    port = DEFAULT_PORTS[protocol]
  end
  if not uri or uri == '' then uri = '/' end
  return protocol, host, tonumber(port), uri
end

local generate_key = function()
  local r1 = mrandom(0,0xfffffff)
  local r2 = mrandom(0,0xfffffff)
  local r3 = mrandom(0,0xfffffff)
  local r4 = mrandom(0,0xfffffff)
  local key = write_int32(r1)..write_int32(r2)..write_int32(r3)..write_int32(r4)
  assert(#key==16,#key)
  return base64_encode(key)
end

return {
  sha1 = sha1_crypto,
  base64 = {
    encode = base64_encode
  },
  parse_url = parse_url,
  generate_key = generate_key,
  read_int8 = read_int8,
  read_int16 = read_int16,
  read_int32 = read_int32,
  write_int8 = write_int8,
  write_int16 = write_int16,
  write_int32 = write_int32,
}

