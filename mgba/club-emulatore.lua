

console:log("Starting websocket server")

local LinkStatus = {

  AwaitMode = 0xFF02,
  HandshakeReceived = 0xFF03,
  HandshakeFinished = 0xFF04,

  LinkConnected = 0xFF05,
  LinkReconnecting = 0xFF06,
  LinkClosed = 0xFF07,

  DeviceReady = 0xFF08,
  EmuTradeSessionFinished = 0xFF09,

  StatusDebug = 0xFFFF
}

local CommandType = {
  SetMode = 0x00,
  Cancel = 0x01,
  SetModeMaster = 0x10,
  SetModeSlave = 0x11,
  StartHandshake = 0x12,
  ConnectLink = 0x13
}

local Transive = {
  HANDSHAKE = 0,
  CRC = 1,
  COMMAND = 2
}

local create_celio_session = function (ws)

  local create_state = function()
    local state = {
      _server = LinkStatus.AwaitMode,
      _client = LinkStatus.AwaitMode,
      _server_ready_reconnect = false,
      _client_ready_reconnect = false,
      _keep_alive = true,
      _transive_state = Transive.HANDSHAKE,
      _received_queue = {},
      _transmit_queue = {},
      _current_tx_command = {},
      _current_rx_command = {}
    }
    return state
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  local celio_session = {
    state = create_state(),
    _ws = ws
  }

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_session:transive(rx_value)
    if (celio_session.state._transive_state == Transive.HANDSHAKE) then
      return celio_session:transive_handshake(rx_value)
    end
    if (celio_session.state._transive_state == Transive.CRC) then
      return celio_session:transive_crc(rx_value)
    end
    if (celio_session.state._transive_state == Transive.COMMAND) then
      return celio_session:transive_command(rx_value)
    end
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_session:checkSendStartHandshake()
    if (celio_session.state._server == LinkStatus.HandshakeReceived and celio_session.state._client == LinkStatus.HandshakeReceived) then
      celio_session._ws:send(tostring(CommandType.StartHandshake))
    end
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_session:transive_handshake(rx_value)
    if (rx_value == 0xB9A0) then
      if (celio_session.state._server == LinkStatus.AwaitMode) then
        celio_session.state._server = LinkStatus.HandshakeReceived
        celio_session:checkSendStartHandshake()
      end
    end

    if (rx_value == 0x8FFF) then
      celio_session._ws:send(tostring(CommandType.ConnectLink))
      celio_session.state._transive_state = Transive.CRC
      celio_session.state._server = LinkStatus.LinkConnected
    end

    if ((celio_session.state._server == LinkStatus.HandshakeReceived or celio_session.state._server == LinkStatus.LinkConnected)
        and (celio_session.state._client == LinkStatus.HandshakeReceived or celio_session.state._client == LinkStatus.LinkConnected)) then
      return 0xB9A0
    end

    return  0xD15E
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  local flush_rx_queue = function ()
    console:log("Flusing rx queue")
    for i = #celio_session.state._transmit_queue, 32 do
      table.insert(celio_session.state._transmit_queue, 0x00)
    end
    local fmt = ">" .. string.rep("I2", #celio_session.state._transmit_queue)
    local data = string.pack(fmt, table.unpack(celio_session.state._transmit_queue))
    celio_session.state._transmit_queue = {}
    celio_session._ws:send(data, require'websocket'.BINARY)
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_session:transive_crc(rx_value)

    if (celio_session.state._client_ready_reconnect and celio_session.state._server_ready_reconnect) then
      console:log("Reconnecting...")
      flush_rx_queue()
      celio_session.state = create_state()
    else
      celio_session.state._transive_state = Transive.COMMAND
    end
    return rx_value
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  local print_command_db = function (prefix, command)
    local print_command = false
    for i = 1, #command do
      if command[i] ~= 0x00 then
        print_command = true
        break
      end
    end

    local command_string = ""
    if (print_command) then
      for i = 1, #command do
        command_string = command_string .. string.format("0x%x", tonumber(command[i])) .. " "
      end
      console:log(prefix .. command_string)
    end
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  local load_tx_command = function ()
    if (#celio_session.state._received_queue == 0) then
      celio_session.state._current_tx_command = {0,0,0,0,0,0,0,0}
    else
      for i = 1, 8 do
        local dequeued_value = table.remove(celio_session.state._received_queue, 1)
        local swapped = ((dequeued_value >> 8) | (dequeued_value << 8)) & 0xFFFF
        table.insert(celio_session.state._current_tx_command, swapped)
      end
    end
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  local save_rx_command = function ()

    -- queue_command if not all zero or if a command is already in queue to avoid stalling
    local queue_command = false
    for i = 1, #celio_session.state._current_rx_command do
      if celio_session.state._current_rx_command[i] ~= 0x00 then
        queue_command = true
        break
      end
    end

    if (#celio_session.state._transmit_queue > 0) then
      queue_command = true
    end

    if (queue_command) then
      for i = 1, 8 do
        local dequeued_value = table.remove(celio_session.state._current_rx_command, 1)
        table.insert(celio_session.state._transmit_queue, dequeued_value)
      end
    end

    celio_session.state._current_rx_command = {}
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_session:transive_command(rx_value)

    table.insert(celio_session.state._current_rx_command, rx_value)

    if (#celio_session.state._current_tx_command == 0) then
      load_tx_command()
      if (celio_session.state._current_tx_command[1] == 0x5fff) then
        console:log("Client ready for reconnect")
        celio_session.state._client_ready_reconnect = true
      end
      print_command_db("tx command ", celio_session.state._current_tx_command)
    end

    if (#celio_session.state._current_rx_command == 8) then
      print_command_db("rx command ", celio_session.state._current_rx_command)

      if (celio_session.state._current_rx_command[1] == 0x5fff) then
        console:log("Server ready for reconnect")
        celio_session.state._server_ready_reconnect = true
      end
      save_rx_command()

      celio_session.state._transive_state = Transive.CRC
    end

    if (#celio_session.state._transmit_queue >= 32) then
      flush_rx_queue()
    end

    return table.remove(celio_session.state._current_tx_command, 1)
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_session:receive_status(status)
    if (status == LinkStatus.AwaitMode) then
      console:log("Received status: AwaitMode")
      console:log(tostring(celio_session._ws))
      celio_session._ws:send(tostring(CommandType.SetModeMaster))

    elseif (status == LinkStatus.HandshakeReceived) then
      console:log("Received status: HandshakeReceived")
      celio_session.state._client = LinkStatus.HandshakeReceived
      celio_session:checkSendStartHandshake()

    elseif (status == LinkStatus.LinkConnected) then
      console:log("Received status: LinkConnected")
      celio_session.state._client = LinkStatus.LinkConnected

    elseif (status == LinkStatus.LinkClosed) then
      console:log("Received status: LinkClosed")
      celio_session.state._client = LinkStatus.LinkConnected
    end
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_session:receive_data(data)
    for i = 1, #data, 2 do
      local value = string.unpack(">I2", data, i)
      table.insert(celio_session.state._received_queue, value)
      end
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  return celio_session
end

local create_celio_client = function(ws)

  console:log("Instanciating new client")
  local celio_client = {
    _ws = ws,
    _session = create_celio_session(ws)
  }

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_client:receive_status(status)
    celio_client._session:receive_status(status)
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  function celio_client:receive_data(data)
    if #data % 16 ~= 0 then
      console:error("Data is not a multiply of 16, data size: " .. #data)
      return
    end
    celio_client._session:receive_data(data)
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  return celio_client
end

local celio_client = nil

if (emu == nil) then
  console:error("Celio mGBA Lua could not find mGBA's global emu API.")
  console:error("Open the ROM in mGBA, then load this script from mGBA's scripting menu instead of running it with standalone Lua.")
  return
end

-- Risolvi la costante WATCHPOINT_READ in modo compatibile con varie versioni di mGBA.
-- Build differenti espongono la costante in posizioni diverse; in alcune build
-- la costante non e' esposta affatto e si deve passare direttamente il valore numerico.
-- Valori canonici (mWatchpointType): WRITE=1, READ=2, RW=3, CHANGE=4, WRITE_CHANGE=5.
local WATCHPOINT_READ_VALUE = 2
local WATCHPOINT_WRITE_VALUE = 1
if (type(C) == "table") then
  if (type(C.WATCHPOINT_TYPE) == "table" and C.WATCHPOINT_TYPE.READ ~= nil) then
    WATCHPOINT_READ_VALUE = C.WATCHPOINT_TYPE.READ
    if (C.WATCHPOINT_TYPE.WRITE ~= nil) then WATCHPOINT_WRITE_VALUE = C.WATCHPOINT_TYPE.WRITE end
  elseif (type(C.mCORE_WATCHPOINT_TYPE) == "table" and C.mCORE_WATCHPOINT_TYPE.READ ~= nil) then
    WATCHPOINT_READ_VALUE = C.mCORE_WATCHPOINT_TYPE.READ
    if (C.mCORE_WATCHPOINT_TYPE.WRITE ~= nil) then WATCHPOINT_WRITE_VALUE = C.mCORE_WATCHPOINT_TYPE.WRITE end
  elseif (type(C.mWATCHPOINT_TYPE) == "table" and C.mWATCHPOINT_TYPE.READ ~= nil) then
    WATCHPOINT_READ_VALUE = C.mWATCHPOINT_TYPE.READ
    if (C.mWATCHPOINT_TYPE.WRITE ~= nil) then WATCHPOINT_WRITE_VALUE = C.mWATCHPOINT_TYPE.WRITE end
  elseif (C.WATCHPOINT_READ ~= nil) then
    WATCHPOINT_READ_VALUE = C.WATCHPOINT_READ
    if (C.WATCHPOINT_WRITE ~= nil) then WATCHPOINT_WRITE_VALUE = C.WATCHPOINT_WRITE end
  end
end

local use_tx_writepoint = false
local mgba_tx_value = 0
local mgba_tx_write_count = 0
local mgba_tx_log_count = 0
local tx_writepoint_callback = function ()
    mgba_tx_value = emu:read16(0x400012A)
    mgba_tx_write_count = mgba_tx_write_count + 1
    if (mgba_tx_log_count < 40 and (mgba_tx_value ~= 0x0000 or mgba_tx_write_count <= 8)) then
      mgba_tx_log_count = mgba_tx_log_count + 1
      console:log(string.format(
        "Celio mGBA Lua SIOMLT_SEND write #%d value=0x%04x",
        mgba_tx_write_count,
        mgba_tx_value
      ))
    end
  end

local watchpoint_count = 0
local watchpoint_log_count = 0
local watchpoint_callback = function ()
    if (celio_client == nil) then return end
    local rx_value_current = use_tx_writepoint and mgba_tx_value or emu:read16(0x400012A)
    local tx_value_current = celio_client._session:transive(rx_value_current)
    watchpoint_count = watchpoint_count + 1
    if (watchpoint_log_count < 40 and (rx_value_current ~= 0x0000 or watchpoint_count <= 8)) then
      watchpoint_log_count = watchpoint_log_count + 1
      console:log(string.format(
        "Celio mGBA Lua SIOMLT_RECV read #%d rx=0x%04x tx=0x%04x state=%d client=0x%04x server=0x%04x writes=%d",
        watchpoint_count,
        rx_value_current,
        tx_value_current,
        celio_client._session.state._transive_state,
        celio_client._session.state._client,
        celio_client._session.state._server,
        mgba_tx_write_count
      ))
    end
    emu:write16(0x4000120, rx_value_current)
    emu:write16(0x4000122, tx_value_current)
    emu:write16(0x4000124, 0xFFFF)
    emu:write16(0x4000126, 0xFFFF)
  end

local writeOk, writeWatchpointId = pcall(function ()
  return emu:setWatchpoint(tx_writepoint_callback, 0x400012A, WATCHPOINT_WRITE_VALUE)
end)
if (not writeOk) then
  writeOk, writeWatchpointId = pcall(function ()
    return emu:setWatchpoint(tx_writepoint_callback, 0x400012A, WATCHPOINT_WRITE_VALUE, -1)
  end)
end
if (writeOk) then
  use_tx_writepoint = true
else
  console:error("Celio mGBA Lua: impossibile registrare il watchpoint SIOMLT_SEND write, uso lettura diretta: " .. tostring(writeWatchpointId))
end

local ok, watchpointId = pcall(function ()
  return emu:setWatchpoint(watchpoint_callback, 0x4000120, WATCHPOINT_READ_VALUE)
end)
if (not ok) then
  -- Alcune build richiedono il parametro segment esplicito (-1 = default)
  ok, watchpointId = pcall(function ()
    return emu:setWatchpoint(watchpoint_callback, 0x4000120, WATCHPOINT_READ_VALUE, -1)
  end)
end
if (not ok) then
  console:error("Celio mGBA Lua: impossibile registrare il watchpoint: " .. tostring(watchpointId))
  return
end
if (use_tx_writepoint) then
  console:log("Celio mGBA Lua installed SIOMLT_SEND write and SIOMLT_RECV read watchpoints")
else
  console:log("Celio mGBA Lua installed SIOMLT_RECV read watchpoint with direct SIOMLT_SEND read fallback")
end

local server = require'websocket'.server.listen
{
  port = 51784,
  protocols = {
    celio = function(ws)

      celio_client = create_celio_client(ws)

      ws:set_on_message(function(ws, message, opcode)
        if (opcode == require'websocket'.TEXT) then
          console:log("Received raw status: " .. string.format("0x%x", tonumber(message)))
          celio_client:receive_status(tonumber(message))
        elseif (opcode == require'websocket'.BINARY) then
          celio_client:receive_data(message)
        end
      end)
    end
  }
}
