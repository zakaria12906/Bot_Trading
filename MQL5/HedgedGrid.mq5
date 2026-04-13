//+------------------------------------------------------------------+
//|                                                   HedgedGrid.mq5 |
//|                        Hedged Grid Bot — MQL5 Expert Advisor      |
//|                                                                    |
//|  Reverse-engineered from live XAUUSDs screenshots (2026-04-13)    |
//|                                                                    |
//|  LOGIC:                                                            |
//|  1. Open BUY 0.01 + SELL 0.01 simultaneously                     |
//|  2. Price drops by GridStep → BUY (recovery lot) + SELL (hedge)   |
//|  3. Price rises by GridStep → SELL (recovery lot) + BUY (hedge)   |
//|  4. Recovery direction locks on first trigger                      |
//|  5. Net P/L >= BasketTP → close ALL → repeat                     |
//|                                                                    |
//|  Lot sequence: 0.01, 0.01, 0.02, 0.03, 0.05, 0.07, 0.11,       |
//|                0.17, 0.25                                          |
//|                                                                    |
//|  HOW TO TEST:                                                      |
//|  1. Copy this file to: MT5\MQL5\Experts\HedgedGrid.mq5           |
//|  2. Open MT5 → Strategy Tester (Ctrl+R)                           |
//|  3. Select "HedgedGrid" → XAUUSD → Period M1 → 3 years           |
//|  4. Mode: "Every tick based on real ticks" for best accuracy      |
//|  5. Click "Start"                                                  |
//+------------------------------------------------------------------+

#property copyright "Hedged Grid Bot"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| INPUT PARAMETERS (adjustable in Strategy Tester)                  |
//+------------------------------------------------------------------+
input double   BaseLot        = 0.01;    // Base lot size
input int      MaxLevels      = 9;       // Maximum grid depth (0-8)
input double   GridStep       = 5.0;     // Points between levels
input double   BasketTP       = 15.0;    // Close basket when net P/L >= this ($)
input int      MagicNumber    = 888001;  // Unique ID for this bot's trades
input int      SessionStart   = 1;       // Start hour (UTC)
input int      SessionEnd     = 22;      // End hour (UTC)
input double   MaxDrawdown    = 500.0;   // Emergency close basket if floating loss > this ($)
input bool     CloseEndOfDay  = false;   // Force close basket at SessionEnd
input int      Slippage       = 30;      // Max slippage in points

//+------------------------------------------------------------------+
//| GLOBAL VARIABLES                                                  |
//+------------------------------------------------------------------+
CTrade trade;

// Lot sequence — exact match to live bot screenshots
double LotSequence[9] = {0.01, 0.01, 0.02, 0.03, 0.05, 0.07, 0.11, 0.17, 0.25};

// State
int    g_currentLevel    = 0;
int    g_recoveryDir     = -1;     // -1=none, 0=BUY, 1=SELL
double g_lastBuyPrice    = 0.0;
double g_lastSellPrice   = 0.0;
bool   g_basketActive    = false;
int    g_totalCycles      = 0;
double g_totalProfit      = 0.0;

//+------------------------------------------------------------------+
//| Expert initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   // Build lot sequence based on BaseLot if different from 0.01
   if(BaseLot != 0.01)
   {
      double mults[9] = {1, 1, 2, 3, 5, 7, 11, 17, 25};
      for(int i = 0; i < 9; i++)
      {
         LotSequence[i] = NormalizeDouble(BaseLot * mults[i], 2);
         LotSequence[i] = MathMax(LotSequence[i], SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));
      }
   }

   // Log startup
   string lots = "";
   int levels = MathMin(MaxLevels, 9);
   for(int i = 0; i < levels; i++)
   {
      if(i > 0) lots += ", ";
      lots += DoubleToString(LotSequence[i], 2);
   }
   Print("=== HEDGED GRID BOT STARTED ===");
   Print("Symbol: ", _Symbol, " | Step: ", GridStep, " | TP: $", BasketTP);
   Print("Max levels: ", MaxLevels, " | Lots: [", lots, "]");
   Print("Session: ", SessionStart, ":00 - ", SessionEnd, ":00 UTC");

   // Check if we already have open positions (restart recovery)
   if(CountMyPositions() > 0)
   {
      g_basketActive = true;
      RecoverState();
      Print("Recovered existing basket: ", CountMyPositions(), " positions");
   }

   // Chart dashboard
   if(!MQLInfoInteger(MQL_TESTER))
      CreateDashboard();

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("=== HEDGED GRID BOT STOPPED ===");
   Print("Total cycles: ", g_totalCycles, " | Total profit: $", 
         DoubleToString(g_totalProfit, 2));
}

//+------------------------------------------------------------------+
//| Expert tick function — called on every price change               |
//+------------------------------------------------------------------+
void OnTick()
{
   // Session filter
   MqlDateTime dt;
   TimeCurrent(dt);
   bool inSession = (dt.hour >= SessionStart && dt.hour < SessionEnd);

   // No basket → open initial pair if in session
   if(!g_basketActive)
   {
      if(inSession)
         OpenInitialPair();
      return;
   }

   // Basket active → check net P/L
   double netPnL = GetBasketPnL();

   // EXIT: net P/L >= target → take profit
   if(netPnL >= BasketTP)
   {
      CloseAllPositions("TAKE_PROFIT");
      g_totalCycles++;
      g_totalProfit += netPnL;

      Print("CYCLE #", g_totalCycles, " CLOSED [TP] | Net P/L: $",
            DoubleToString(netPnL, 2), " | Total: $",
            DoubleToString(g_totalProfit, 2));

      ResetState();

      if(inSession)
         OpenInitialPair();
      return;
   }

   // SAFETY: max drawdown breached → emergency close
   if(MaxDrawdown > 0 && netPnL <= -MaxDrawdown)
   {
      CloseAllPositions("MAX_DRAWDOWN");
      g_totalCycles++;
      g_totalProfit += netPnL;

      Print("CYCLE #", g_totalCycles, " CLOSED [DD STOP] | Net P/L: $",
            DoubleToString(netPnL, 2), " | Total: $",
            DoubleToString(g_totalProfit, 2));

      ResetState();
      return;  // Do NOT reopen after emergency stop
   }

   // SESSION END: force close basket if configured
   if(CloseEndOfDay && !inSession)
   {
      CloseAllPositions("SESSION_END");
      g_totalCycles++;
      g_totalProfit += netPnL;

      Print("CYCLE #", g_totalCycles, " CLOSED [SESSION END] | Net P/L: $",
            DoubleToString(netPnL, 2), " | Total: $",
            DoubleToString(g_totalProfit, 2));

      ResetState();
      return;
   }

   // CHECK: next grid level
   if(g_currentLevel < MaxLevels - 1)
      CheckNextLevel();

   // Update visual dashboard
   UpdateDashboard();
}

//+------------------------------------------------------------------+
//| Open the initial BUY + SELL hedge pair                            |
//+------------------------------------------------------------------+
void OpenInitialPair()
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(ask == 0 || bid == 0)
      return;

   // Open BUY
   if(!trade.Buy(BaseLot, _Symbol, ask, 0, 0, "HG_L0_BUY"))
   {
      Print("Initial BUY failed: ", trade.ResultRetcodeDescription());
      return;
   }

   // Open SELL
   if(!trade.Sell(BaseLot, _Symbol, bid, 0, 0, "HG_L0_SELL"))
   {
      Print("Initial SELL failed: ", trade.ResultRetcodeDescription());
      // Close the BUY we just opened
      CloseAllPositions("INIT_FAIL");
      return;
   }

   g_lastBuyPrice  = ask;
   g_lastSellPrice = bid;
   g_currentLevel  = 0;
   g_recoveryDir   = -1;
   g_basketActive  = true;

   Print("CYCLE OPENED | BUY ", DoubleToString(BaseLot, 2), " @ ",
         DoubleToString(ask, _Digits), " | SELL ", DoubleToString(BaseLot, 2),
         " @ ", DoubleToString(bid, _Digits));
}

//+------------------------------------------------------------------+
//| Check if the next grid level should be triggered                  |
//+------------------------------------------------------------------+
void CheckNextLevel()
{
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

   if(bid == 0 || ask == 0)
      return;

   double buyTrigger  = g_lastBuyPrice - GridStep;
   double sellTrigger = g_lastSellPrice + GridStep;

   // Recovery direction not set yet — check both sides
   if(g_recoveryDir == -1)
   {
      if(bid <= buyTrigger && ask >= sellTrigger)
      {
         // Both triggered — pick the one with more distance
         if((g_lastBuyPrice - bid) >= (ask - g_lastSellPrice))
            AddLevelBuyRecovery();
         else
            AddLevelSellRecovery();
      }
      else if(bid <= buyTrigger)
         AddLevelBuyRecovery();
      else if(ask >= sellTrigger)
         AddLevelSellRecovery();
   }
   // BUY is recovery → only trigger when price drops further
   else if(g_recoveryDir == 0)
   {
      if(bid <= buyTrigger)
         AddLevelBuyRecovery();
   }
   // SELL is recovery → only trigger when price rises further
   else if(g_recoveryDir == 1)
   {
      if(ask >= sellTrigger)
         AddLevelSellRecovery();
   }
}

//+------------------------------------------------------------------+
//| Add BUY recovery level (price dropped)                            |
//+------------------------------------------------------------------+
void AddLevelBuyRecovery()
{
   int nextLevel = g_currentLevel + 1;
   if(nextLevel >= MaxLevels)
      return;

   if(g_recoveryDir == -1)
      g_recoveryDir = 0;  // BUY is recovery

   double recoveryLot = (g_recoveryDir == 0) ? GetLot(nextLevel) : BaseLot;
   double hedgeLot    = BaseLot;

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   // Recovery BUY (bigger lot)
   if(!trade.Buy(recoveryLot, _Symbol, ask, 0, 0,
                 "HG_L" + IntegerToString(nextLevel) + "_BUY"))
   {
      Print("Level ", nextLevel, " BUY failed: ", trade.ResultRetcodeDescription());
      return;
   }
   g_lastBuyPrice = ask;

   // Hedge SELL (base lot)
   if(trade.Sell(hedgeLot, _Symbol, bid, 0, 0,
                 "HG_L" + IntegerToString(nextLevel) + "_SELL"))
   {
      g_lastSellPrice = bid;
   }

   g_currentLevel = nextLevel;

   Print("LEVEL ", nextLevel, " (BUY recovery) | BUY ",
         DoubleToString(recoveryLot, 2), " @ ", DoubleToString(ask, _Digits),
         " | SELL ", DoubleToString(hedgeLot, 2), " @ ",
         DoubleToString(bid, _Digits),
         " | Net: $", DoubleToString(GetBasketPnL(), 2));
}

//+------------------------------------------------------------------+
//| Add SELL recovery level (price rose)                              |
//+------------------------------------------------------------------+
void AddLevelSellRecovery()
{
   int nextLevel = g_currentLevel + 1;
   if(nextLevel >= MaxLevels)
      return;

   if(g_recoveryDir == -1)
      g_recoveryDir = 1;  // SELL is recovery

   double recoveryLot = (g_recoveryDir == 1) ? GetLot(nextLevel) : BaseLot;
   double hedgeLot    = BaseLot;

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   // Recovery SELL (bigger lot)
   if(!trade.Sell(recoveryLot, _Symbol, bid, 0, 0,
                  "HG_L" + IntegerToString(nextLevel) + "_SELL"))
   {
      Print("Level ", nextLevel, " SELL failed: ", trade.ResultRetcodeDescription());
      return;
   }
   g_lastSellPrice = bid;

   // Hedge BUY (base lot)
   if(trade.Buy(hedgeLot, _Symbol, ask, 0, 0,
                "HG_L" + IntegerToString(nextLevel) + "_BUY"))
   {
      g_lastBuyPrice = ask;
   }

   g_currentLevel = nextLevel;

   Print("LEVEL ", nextLevel, " (SELL recovery) | SELL ",
         DoubleToString(recoveryLot, 2), " @ ", DoubleToString(bid, _Digits),
         " | BUY ", DoubleToString(hedgeLot, 2), " @ ",
         DoubleToString(ask, _Digits),
         " | Net: $", DoubleToString(GetBasketPnL(), 2));
}

//+------------------------------------------------------------------+
//| Get lot size for a given level from the sequence                  |
//+------------------------------------------------------------------+
double GetLot(int level)
{
   if(level < 0 || level >= 9)
      return BaseLot;

   double lot = LotSequence[level];

   // Clamp to broker limits
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   lot = MathMax(lot, minLot);
   lot = MathMin(lot, maxLot);
   lot = MathFloor(lot / lotStep) * lotStep;
   lot = NormalizeDouble(lot, 2);

   return lot;
}

//+------------------------------------------------------------------+
//| Calculate net P/L of all positions with our magic number          |
//+------------------------------------------------------------------+
double GetBasketPnL()
{
   double total = 0.0;
   int count = PositionsTotal();

   for(int i = 0; i < count; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;

      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

      total += PositionGetDouble(POSITION_PROFIT)
             + PositionGetDouble(POSITION_SWAP)
             + PositionGetDouble(POSITION_COMMISSION);
   }

   return total;
}

//+------------------------------------------------------------------+
//| Count positions belonging to this bot                             |
//+------------------------------------------------------------------+
int CountMyPositions()
{
   int count = 0;
   int total = PositionsTotal();

   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;

      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      count++;
   }

   return count;
}

//+------------------------------------------------------------------+
//| Close all positions belonging to this bot                         |
//+------------------------------------------------------------------+
void CloseAllPositions(string reason)
{
   int total = PositionsTotal();

   // Close in reverse order to avoid index shifting
   for(int i = total - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;

      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

      if(!trade.PositionClose(ticket, Slippage))
      {
         Print("Failed to close ticket ", ticket, ": ",
               trade.ResultRetcodeDescription());
      }
   }
}

//+------------------------------------------------------------------+
//| Reset state for a new cycle                                       |
//+------------------------------------------------------------------+
void ResetState()
{
   g_currentLevel  = 0;
   g_recoveryDir   = -1;
   g_lastBuyPrice  = 0.0;
   g_lastSellPrice = 0.0;
   g_basketActive  = false;
}

//+------------------------------------------------------------------+
//| Recover state from existing positions (after restart)             |
//+------------------------------------------------------------------+
void RecoverState()
{
   int total = PositionsTotal();
   double highestBuy  = 0;
   double lowestBuy   = 999999;
   double highestSell = 0;
   double lowestSell  = 999999;
   int buyCount = 0, sellCount = 0;
   double maxBuyLot = 0, maxSellLot = 0;

   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double volume    = PositionGetDouble(POSITION_VOLUME);
      int    type      = (int)PositionGetInteger(POSITION_TYPE);

      if(type == POSITION_TYPE_BUY)
      {
         buyCount++;
         if(openPrice > highestBuy)  highestBuy  = openPrice;
         if(openPrice < lowestBuy)   lowestBuy   = openPrice;
         if(volume > maxBuyLot)      maxBuyLot   = volume;
      }
      else
      {
         sellCount++;
         if(openPrice > highestSell) highestSell = openPrice;
         if(openPrice < lowestSell)  lowestSell  = openPrice;
         if(volume > maxSellLot)     maxSellLot  = volume;
      }
   }

   // Determine recovery direction from lot sizes
   if(maxBuyLot > maxSellLot)
   {
      g_recoveryDir   = 0;  // BUY is recovery
      g_lastBuyPrice  = lowestBuy;
      g_lastSellPrice = lowestSell;
   }
   else if(maxSellLot > maxBuyLot)
   {
      g_recoveryDir   = 1;  // SELL is recovery
      g_lastBuyPrice  = highestBuy;
      g_lastSellPrice = highestSell;
   }
   else
   {
      g_recoveryDir   = -1;
      g_lastBuyPrice  = highestBuy;
      g_lastSellPrice = lowestSell;
   }

   // Estimate current level from position count
   int posCount = buyCount + sellCount;
   g_currentLevel = MathMax(0, (posCount / 2) - 1);
   g_basketActive = true;
}

//+------------------------------------------------------------------+
//| On-chart dashboard (live mode only, not in tester)                |
//+------------------------------------------------------------------+
void CreateDashboard()
{
   string prefix = "HG_";
   int x = 10, y = 30, gap = 18;
   color txtColor = clrWhite;
   int fontSize = 9;

   ObjectCreate(0, prefix + "bg", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_XDISTANCE, 5);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_YDISTANCE, 25);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_XSIZE, 260);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_YSIZE, 180);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_BGCOLOR, C'30,30,40');
   ObjectSetInteger(0, prefix + "bg", OBJPROP_BORDER_COLOR, clrDodgerBlue);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_CORNER, CORNER_LEFT_UPPER);

   string labels[] = {"title", "status", "level", "positions",
                       "pnl", "cycles", "total", "dd_limit"};
   for(int i = 0; i < ArraySize(labels); i++)
   {
      string name = prefix + labels[i];
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
      ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y + i * gap);
      ObjectSetInteger(0, name, OBJPROP_COLOR, txtColor);
      ObjectSetInteger(0, name, OBJPROP_FONTSIZE, fontSize);
      ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   }

   ObjectSetString(0, prefix + "title", OBJPROP_TEXT, "═══ HEDGED GRID BOT ═══");
   ObjectSetInteger(0, prefix + "title", OBJPROP_COLOR, clrDodgerBlue);
}

void UpdateDashboard()
{
   if(MQLInfoInteger(MQL_TESTER))
      return;

   string prefix = "HG_";
   double pnl = g_basketActive ? GetBasketPnL() : 0.0;
   string status = g_basketActive ? "ACTIVE" : "IDLE";
   string recDir = g_recoveryDir == 0 ? "BUY" :
                   g_recoveryDir == 1 ? "SELL" : "---";

   ObjectSetString(0, prefix + "status",    OBJPROP_TEXT,
      "Status:     " + status + "  (Recovery: " + recDir + ")");
   ObjectSetString(0, prefix + "level",     OBJPROP_TEXT,
      "Level:      " + IntegerToString(g_currentLevel) + " / " + IntegerToString(MaxLevels - 1));
   ObjectSetString(0, prefix + "positions", OBJPROP_TEXT,
      "Positions:  " + IntegerToString(CountMyPositions()));

   color pnlColor = pnl >= 0 ? clrLime : clrRed;
   ObjectSetString(0, prefix + "pnl",      OBJPROP_TEXT,
      "Net P/L:    $" + DoubleToString(pnl, 2));
   ObjectSetInteger(0, prefix + "pnl",     OBJPROP_COLOR, pnlColor);

   ObjectSetString(0, prefix + "cycles",   OBJPROP_TEXT,
      "Cycles:     " + IntegerToString(g_totalCycles));
   ObjectSetString(0, prefix + "total",    OBJPROP_TEXT,
      "Total P/L:  $" + DoubleToString(g_totalProfit, 2));
   ObjectSetString(0, prefix + "dd_limit", OBJPROP_TEXT,
      "DD Limit:   $" + DoubleToString(MaxDrawdown, 2));

   ChartRedraw();
}
//+------------------------------------------------------------------+
