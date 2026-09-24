-- Derivato da lua-websockets (https://github.com/lipp/lua-websockets)
-- Copyright (c) 2012 by Gerhard Lipp <gelipp@gmail.com> - licenza MIT (testo completo nel
-- file COPYRIGHT di lua-websockets). Preso via Celio-mGBA-Link (GPL-3.0). Vedi THIRD_PARTY_NOTICES.md.

local frame = require'websocket.frame'
local handshake = require'websocket.handshake'
local concat = table.concat
local insert = table.insert

--TODO absorb into client
local message_io = function(sock, on_message, on_error)
  local frames = {}
  local first_opcode

  local self = {}

  self.receive = function()

    local encoded, err = sock:receive(100000)

    if err then
      console:error('WS: Error on receive of package occured ' .. err)
      on_error(err)
      return
    end

    repeat
      local decoded, fin, opcode, rest = frame.decode(encoded)
      if decoded then
        if not first_opcode then
            first_opcode = opcode
        end
        insert(frames,decoded)
        encoded = rest
        if fin == true then
            on_message(concat(frames), first_opcode)
            frames = {}
            first_opcode = nil
        end
      end
    until not decoded

  end

  return self

end

local create_client = function(sock, opts)
  local handler = {
    _sock = sock,
    _receive_state = 'handshake',
    _state = 'OPEN',
    _message_io = nil,
    _callback_received_id = nil,
    _callback_error_id = nil,
    _user_on_close = nil,
    _user_on_error = nil,
    _user_on_message = nil,
    _opts = opts
  }

  handler._callback_received_id = handler._sock:add("received", function()
    if handler._state == 'CLOSED' then return end

    if handler._receive_state == 'handshake' then
      handler:exchange_handshake()
      handler._receive_state = 'frame'
    elseif handler._receive_state == 'frame' then
      handler._message_io:receive()
    end
  end)

  handler._callback_id = handler._sock:add("error", function()
    console:error("WS: An unknown error occured on the client socket")
    handler:handle_sock_err('unknown error')
  end)

  function handler:set_on_close(on_close_arg)
    self._user_on_close = on_close_arg
  end

  function handler:set_on_error(on_error_arg)
    self._user_on_error = on_error_arg
  end

  function handler:set_on_message(on_message_arg)
    self._user_on_message = on_message_arg
  end

  local function on_close(was_clean, code, reason)

    -- set everything to nil for good measure, we are done here
    handler._state = 'CLOSED'
    handler._sock:remove(handler._callback_received_id)
    handler._sock:remove(handler._callback_error_id)
    handler._sock:close()
    handler._sock = nil
    handler._message_io = nil
    handler._user_on_close = nil
    handler._user_on_error = nil
    handler._user_on_message = nil
    handler._opts = nil

    if handler._user_on_close then
      handler._user_on_close(handler, was_clean, code, reason or '')
    end

    console:log("WS: Socket closed, client disconnected")
  end

  local function handle_sock_err(err)
    if err == 'closed' and handler._state ~= 'CLOSED' then
        handler:close()
    else
      on_close(false, 1000, '')
    end
    if handler._user_on_close then
      handler._user_on_error(handler, err)
    end
  end

  function handler:send(message, opcode)
    local encoded = frame.encode(message, opcode or frame.TEXT)
    return sock:send(encoded)
  end

  --FIXME
  function handler:broadcast(...)
  end

  function handler:close(code, reason)
    code = code or 1000
    reason = reason or ''

    if self._state == 'OPEN' then
      self._state = 'CLOSING'
      local encoded = frame.encode_close(code, reason)
      encoded = frame.encode(encoded, frame.CLOSE)
      sock:send(encoded)
    end

    on_close(true, code, reason)
  end

  function handler:on_message(message, opcode)
    if opcode == frame.TEXT or opcode == frame.BINARY then
      self._user_on_message(self, message, opcode)
    elseif opcode == frame.CLOSE then
      if self._state ~= 'CLOSING' then
        self._state = 'CLOSING'
        local code, reason = frame.decode_close(message)
        local encoded = frame.encode_close(code)
        encoded = frame.encode(encoded,frame.CLOSE)
        sock:send(encoded)
        on_close(true, code or 1006, reason)
      else
        on_close(true, 1006, '')
      end
    end
  end

  function handler:exchange_handshake()
    console:log("WS: Handshake received \n")
    local request = {}

    local buffer = self._sock:receive(1024)

    repeat
      local line_end = buffer:find("\r\n", 1, true)
      local line = buffer:sub(1, line_end - 1)
      buffer = buffer:sub(line_end + 2)
      request[#request+1] = line
      console:log(line)
    until line == ''

    local upgrade_request = concat(request,'\r\n')
    local response, protocol = handshake.accept_upgrade(upgrade_request, self._opts.protocols)

    console:log(response)

    if not response then
      console:error('WS: Handshake failed, Request:')
      console:log(upgrade_request)
      self.close(nil, nil)
      return
    end

    local sent, err = self._sock:send(response)

    if err then
      console:log('WS: Websocket client closed while handshake ' .. err)
      self.close(nil, nil)
      return
    end

    self:accept_client(protocol)
  end

  function handler:accept_client(protocol)

    local protocol_handler

    if protocol and opts.protocols[protocol] then
      console:log("WS: Using " .. protocol .. " protocol")
      protocol_handler = opts.protocols[protocol]
    elseif opts.default then
      console:log("WS: Using default protocol")
      protocol_handler = opts.default
    else
      console:error('WS: No Protocol is matching and no default one has been assinged. Closing.')
      handler:close(1006, 'Wrong Protocol')
      return
    end

    protocol_handler(handler)
  end

  handler._message_io = message_io(
    sock,
    function(...)
      console:log("WS: Received frame")
      handler:on_message(...)
    end,
    handle_sock_err
  )

  return handler
end

return {
  create_client = create_client
}