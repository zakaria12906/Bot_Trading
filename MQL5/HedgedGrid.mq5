//+------------------------------------------------------------------+
//|                                                   HedgedGrid.mq5 |
//|                  Hedged Grid Bot v3 — Adaptive Multi-Basket EA    |
//|                                                                    |
//|  KEY UPGRADE over v2:                                              |
//|  - ATR-based adaptive GridStep (auto-scales with volatility)      |
//|  - 5 simultaneous baskets for maximum position density            |
//|  - Per-basket gridStep frozen at creation (stable grid)           |
//|  - Smarter risk: per-basket + total drawdown limits               |
//|  - Targets: 30-80+ positions/day, matching live bot behavior      |
//+------------------------------------------------------------------+

#property copyright "Hedged Grid Bot v3"
#property version   "3.00"
#property strict

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| INPUT PARAMETERS                                                  |
//+------------------------------------------------------------------+

// ── Lot sizing ──
input double   BaseLot          = 0.01;    // Base lot size
input int      MaxLevels        = 6;       // Maximum grid depth per basket (6 safe for $1K)

// ── Grid spacing ──
input bool     UseATR           = true;    // Use ATR for dynamic grid step
input int      ATR_Period       = 14;      // ATR calculation period
input ENUM_TIMEFRAMES ATR_TF   = PERIOD_H1;// ATR timeframe
input double   ATR_GridMult     = 0.50;    // GridStep = ATR × this (when UseATR=true)
input double   ATR_DistMult     = 0.80;    // MinDistance = ATR × this (when UseATR=true)
input double   GridStep         = 5.0;     // Fixed grid step (when UseATR=false)
input double   MinDistance      = 8.0;     // Fixed min distance (when UseATR=false)

// ── Basket profit target ──
input double   BasketTP         = 8.0;     // Close basket when net P/L >= this ($)

// ── Multi-basket ──
input int      MaxBaskets       = 2;       // Max simultaneous baskets (2 safe for $1K)
input int      BaseMagic        = 888000;  // Base magic number

// ── Session ──
input int      SessionStart     = 1;       // Session start hour (server time)
input int      SessionEnd       = 23;      // Session end hour (server time)

// ── Risk management ──
input double   MaxDrawdown      = 200.0;   // Emergency close ONE basket if loss > this ($)
input double   MaxTotalDrawdown = 400.0;   // Emergency close ALL if total loss > this ($)
input double   MinMarginPct     = 30.0;    // Stop opening if free margin < this % of equity
input bool     CloseEndOfDay    = false;   // Force close all at SessionEnd

// ── Execution ──
input int      Slippage         = 30;      // Max slippage in points

//+------------------------------------------------------------------+
//| BASKET STATE                                                      |
//+------------------------------------------------------------------+
struct SBasket
{
   int      magic;
   int      currentLevel;
   int      recoveryDir;      // -1=none, 0=BUY, 1=SELL
   double   lastBuyPrice;
   double   lastSellPrice;
   double   entryMid;
   double   gridStep;         // frozen at basket creation from ATR
   bool     active;
   int      posCount;
   datetime openTime;
};

//+------------------------------------------------------------------+
//| GLOBALS                                                           |
//+------------------------------------------------------------------+
CTrade   trade;
double   LotSeq[9];
SBasket  g_baskets[];
int      g_atrHandle = INVALID_HANDLE;
int      g_totalCycles  = 0;
double   g_totalProfit  = 0.0;
int      g_totalTrades  = 0;

//+------------------------------------------------------------------+
//| OnInit                                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(BaseMagic);
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   // Build lot sequence
   double mults[9] = {1, 1, 2, 3, 5, 7, 11, 17, 25};
   for(int i = 0; i < 9; i++)
   {
      LotSeq[i] = NormalizeDouble(BaseLot * mults[i], 2);
      LotSeq[i] = MathMax(LotSeq[i], SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));
   }

   // ATR indicator handle
   if(UseATR)
   {
      g_atrHandle = iATR(_Symbol, ATR_TF, ATR_Period);
      if(g_atrHandle == INVALID_HANDLE)
      {
         Print("WARNING: ATR handle failed — falling back to fixed GridStep");
      }
   }

   // Initialize baskets
   ArrayResize(g_baskets, MaxBaskets);
   for(int b = 0; b < MaxBaskets; b++)
   {
      ResetBasket(b);
      g_baskets[b].magic = BaseMagic + b + 1;
   }

   RecoverAllBaskets();

   // Log
   string lots = "";
   for(int i = 0; i < MathMin(MaxLevels, 9); i++)
   {
      if(i > 0) lots += ",";
      lots += DoubleToString(LotSeq[i], 2);
   }

   Print("=== HEDGED GRID v3 — ADAPTIVE ===");
   Print(_Symbol, " | ATR: ", UseATR ? "ON" : "OFF",
         " | Baskets: ", MaxBaskets, " | TP: $", BasketTP);
   Print("Lots: [", lots, "] | Levels: ", MaxLevels);
   Print("Session: ", SessionStart, "-", SessionEnd,
         " | DD/basket: $", MaxDrawdown, " | DD/total: $", MaxTotalDrawdown);

   if(UseATR)
      Print("ATR(", ATR_Period, ",", EnumToString(ATR_TF),
            ") × ", ATR_GridMult, " = GridStep | × ", ATR_DistMult, " = MinDist");
   else
      Print("Fixed GridStep: ", GridStep, " | Fixed MinDist: ", MinDistance);

   int active = CountActive();
   if(active > 0) Print("Recovered ", active, " active baskets");

   if(!MQLInfoInteger(MQL_TESTER))
      CreateDashboard();

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| OnDeinit                                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(g_atrHandle != INVALID_HANDLE)
      IndicatorRelease(g_atrHandle);

   Print("=== HEDGED GRID v3 STOPPED ===");
   Print("Cycles: ", g_totalCycles, " | Profit: $",
         DoubleToString(g_totalProfit, 2), " | Trades: ", g_totalTrades);
}

//+------------------------------------------------------------------+
//| Get current ATR value                                             |
//+------------------------------------------------------------------+
double GetATR()
{
   if(g_atrHandle == INVALID_HANDLE) return 0;

   double buf[1];
   if(CopyBuffer(g_atrHandle, 0, 0, 1, buf) > 0)
      return buf[0];
   return 0;
}

//+------------------------------------------------------------------+
//| Check if we have enough free margin to open a position            |
//+------------------------------------------------------------------+
bool HasEnoughMargin(double lot)
{
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);

   if(equity <= 0) return false;

   double freePct = (freeMargin / equity) * 100.0;
   if(freePct < MinMarginPct)
   {
      static datetime lastWarn = 0;
      if(TimeCurrent() - lastWarn > 60)
      {
         Print("MARGIN GUARD: free margin ", DoubleToString(freePct, 1),
               "% < ", DoubleToString(MinMarginPct, 1), "% — skipping open");
         lastWarn = TimeCurrent();
      }
      return false;
   }

   // Also check if broker would accept this trade
   double marginRequired = 0;
   if(!OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lot,
                        SymbolInfoDouble(_Symbol, SYMBOL_ASK), marginRequired))
      return true;  // Can't calculate, let broker decide

   if(marginRequired > freeMargin * 0.5)
      return false;

   return true;
}

//+------------------------------------------------------------------+
//| Get effective grid step (ATR-based or fixed)                      |
//+------------------------------------------------------------------+
double GetEffectiveGridStep()
{
   if(UseATR)
   {
      double atr = GetATR();
      if(atr > 0)
         return NormalizeDouble(atr * ATR_GridMult, _Digits);
   }
   return GridStep;
}

//+------------------------------------------------------------------+
//| Get effective min distance between baskets                        |
//+------------------------------------------------------------------+
double GetEffectiveMinDist()
{
   if(UseATR)
   {
      double atr = GetATR();
      if(atr > 0)
         return NormalizeDouble(atr * ATR_DistMult, _Digits);
   }
   return MinDistance;
}

//+------------------------------------------------------------------+
//| OnTick — Main loop                                                |
//+------------------------------------------------------------------+
void OnTick()
{
   MqlDateTime dt;
   TimeCurrent(dt);
   bool inSession = (dt.hour >= SessionStart && dt.hour < SessionEnd);

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(bid == 0 || ask == 0) return;

   // ── GLOBAL SAFETY ──
   if(MaxTotalDrawdown > 0)
   {
      double totalPnL = GetTotalPnL();
      if(totalPnL <= -MaxTotalDrawdown)
      {
         CloseAllBaskets("TOTAL_DD");
         Print("!!! GLOBAL DD STOP: $", DoubleToString(totalPnL, 2));
         return;
      }
   }

   // ── SESSION END ──
   if(CloseEndOfDay && !inSession && CountActive() > 0)
   {
      CloseAllBaskets("SESSION_END");
      return;
   }

   // ── PROCESS EACH BASKET ──
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;

      double pnl = GetBasketPnL(g_baskets[b].magic);

      // Take profit → close + immediately recycle slot
      if(pnl >= BasketTP)
      {
         int nClosed = CloseBasketPositions(g_baskets[b].magic);
         g_totalCycles++;
         g_totalProfit += pnl;
         g_totalTrades += nClosed;

         Print("B#", b, " TP | Lv", g_baskets[b].currentLevel,
               " | ", nClosed, " pos | $", DoubleToString(pnl, 2),
               " | Cycle #", g_totalCycles,
               " | Total: $", DoubleToString(g_totalProfit, 2));

         ResetBasket(b);
         continue;
      }

      // Per-basket drawdown stop
      if(MaxDrawdown > 0 && pnl <= -MaxDrawdown)
      {
         int nClosed = CloseBasketPositions(g_baskets[b].magic);
         g_totalCycles++;
         g_totalProfit += pnl;
         g_totalTrades += nClosed;

         Print("B#", b, " DD STOP | $", DoubleToString(pnl, 2));
         ResetBasket(b);
         continue;
      }

      // Check next grid level
      if(g_baskets[b].currentLevel < MaxLevels - 1)
         CheckNextLevel(b, bid, ask);
   }

   // ── OPEN NEW BASKETS ──
   if(inSession)
   {
      int active = CountActive();
      while(active < MaxBaskets)
      {
         if(!TryOpenNewBasket(bid, ask))
            break;
         active++;
      }
   }

   UpdateDashboard();
}

//+------------------------------------------------------------------+
//| Try to open a new basket                                          |
//+------------------------------------------------------------------+
bool TryOpenNewBasket(double bid, double ask)
{
   // Margin guard — don't open if not enough free margin
   if(!HasEnoughMargin(BaseLot))
      return false;

   double mid = (bid + ask) / 2.0;
   double minDist = GetEffectiveMinDist();

   // Check distance from all active baskets
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;
      if(MathAbs(mid - g_baskets[b].entryMid) < minDist)
         return false;
   }

   // Find free slot
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active)
      {
         OpenBasket(b, bid, ask);
         return true;
      }
   }
   return false;
}

//+------------------------------------------------------------------+
//| Open a new basket                                                 |
//+------------------------------------------------------------------+
void OpenBasket(int b, double bid, double ask)
{
   int magic = g_baskets[b].magic;
   trade.SetExpertMagicNumber(magic);

   if(!trade.Buy(BaseLot, _Symbol, ask, 0, 0,
                  "HG_B" + IntegerToString(b) + "_L0_BUY"))
   {
      Print("B#", b, " BUY failed: ", trade.ResultRetcodeDescription());
      return;
   }

   if(!trade.Sell(BaseLot, _Symbol, bid, 0, 0,
                   "HG_B" + IntegerToString(b) + "_L0_SELL"))
   {
      Print("B#", b, " SELL failed — closing BUY");
      CloseBasketPositions(magic);
      return;
   }

   // Freeze current ATR-based grid step for this basket's lifetime
   double step = GetEffectiveGridStep();

   g_baskets[b].currentLevel  = 0;
   g_baskets[b].recoveryDir   = -1;
   g_baskets[b].lastBuyPrice  = ask;
   g_baskets[b].lastSellPrice = bid;
   g_baskets[b].entryMid      = (bid + ask) / 2.0;
   g_baskets[b].gridStep      = step;
   g_baskets[b].active        = true;
   g_baskets[b].posCount      = 2;
   g_baskets[b].openTime      = TimeCurrent();

   Print("B#", b, " OPENED @ ", DoubleToString((bid+ask)/2.0, _Digits),
         " | step=", DoubleToString(step, 2),
         " | Active: ", CountActive());
}

//+------------------------------------------------------------------+
//| Check if next grid level should trigger                           |
//+------------------------------------------------------------------+
void CheckNextLevel(int b, double bid, double ask)
{
   double step        = g_baskets[b].gridStep;
   double buyTrigger  = g_baskets[b].lastBuyPrice  - step;
   double sellTrigger = g_baskets[b].lastSellPrice + step;
   int    recDir      = g_baskets[b].recoveryDir;

   if(recDir == -1)
   {
      if(bid <= buyTrigger && ask >= sellTrigger)
      {
         if((g_baskets[b].lastBuyPrice - bid) >= (ask - g_baskets[b].lastSellPrice))
            AddLevel(b, 0, bid, ask);  // BUY recovery
         else
            AddLevel(b, 1, bid, ask);  // SELL recovery
      }
      else if(bid <= buyTrigger)
         AddLevel(b, 0, bid, ask);
      else if(ask >= sellTrigger)
         AddLevel(b, 1, bid, ask);
   }
   else if(recDir == 0 && bid <= buyTrigger)
      AddLevel(b, 0, bid, ask);
   else if(recDir == 1 && ask >= sellTrigger)
      AddLevel(b, 1, bid, ask);
}

//+------------------------------------------------------------------+
//| Add a grid level (direction: 0=BUY recovery, 1=SELL recovery)    |
//+------------------------------------------------------------------+
void AddLevel(int b, int recovDir, double bid, double ask)
{
   int nextLv = g_baskets[b].currentLevel + 1;
   if(nextLv >= MaxLevels) return;

   // Margin guard before adding more positions
   double nextLot = GetLot(nextLv);
   if(!HasEnoughMargin(nextLot + BaseLot))
      return;

   if(g_baskets[b].recoveryDir == -1)
      g_baskets[b].recoveryDir = recovDir;

   int magic = g_baskets[b].magic;
   trade.SetExpertMagicNumber(magic);

   double recLot   = (g_baskets[b].recoveryDir == recovDir) ? GetLot(nextLv) : BaseLot;
   double hedgeLot = BaseLot;
   string tag = "HG_B" + IntegerToString(b) + "_L" + IntegerToString(nextLv);

   if(recovDir == 0)
   {
      // BUY recovery (price dropping)
      if(!trade.Buy(recLot, _Symbol, ask, 0, 0, tag + "_BUY"))
      { Print("B#", b, " L", nextLv, " BUY fail"); return; }
      g_baskets[b].lastBuyPrice = ask;
      g_baskets[b].posCount++;

      if(trade.Sell(hedgeLot, _Symbol, bid, 0, 0, tag + "_SELL"))
      { g_baskets[b].lastSellPrice = bid; g_baskets[b].posCount++; }
   }
   else
   {
      // SELL recovery (price rising)
      if(!trade.Sell(recLot, _Symbol, bid, 0, 0, tag + "_SELL"))
      { Print("B#", b, " L", nextLv, " SELL fail"); return; }
      g_baskets[b].lastSellPrice = bid;
      g_baskets[b].posCount++;

      if(trade.Buy(hedgeLot, _Symbol, ask, 0, 0, tag + "_BUY"))
      { g_baskets[b].lastBuyPrice = ask; g_baskets[b].posCount++; }
   }

   g_baskets[b].currentLevel = nextLv;

   string dir = recovDir == 0 ? "BUY" : "SELL";
   Print("B#", b, " Lv", nextLv, " ", dir, " rec | ",
         DoubleToString(recLot, 2), "+", DoubleToString(hedgeLot, 2),
         " | $", DoubleToString(GetBasketPnL(magic), 2));
}

//+------------------------------------------------------------------+
//| Get lot from sequence (clamped to broker limits)                  |
//+------------------------------------------------------------------+
double GetLot(int level)
{
   if(level < 0 || level >= 9) return BaseLot;

   double lot     = LotSeq[level];
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   lot = MathMax(lot, minLot);
   lot = MathMin(lot, maxLot);
   lot = MathFloor(lot / lotStep) * lotStep;
   return NormalizeDouble(lot, 2);
}

//+------------------------------------------------------------------+
//| P/L calculation for one basket                                    |
//+------------------------------------------------------------------+
double GetBasketPnL(int magic)
{
   double total = 0;
   int cnt = PositionsTotal();
   for(int i = 0; i < cnt; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

      total += PositionGetDouble(POSITION_PROFIT)
             + PositionGetDouble(POSITION_SWAP)
             + PositionGetDouble(POSITION_COMMISSION);
   }
   return total;
}

//+------------------------------------------------------------------+
//| Total P/L across all baskets                                      |
//+------------------------------------------------------------------+
double GetTotalPnL()
{
   double total = 0;
   for(int b = 0; b < MaxBaskets; b++)
      if(g_baskets[b].active)
         total += GetBasketPnL(g_baskets[b].magic);
   return total;
}

//+------------------------------------------------------------------+
//| Count active baskets                                              |
//+------------------------------------------------------------------+
int CountActive()
{
   int c = 0;
   for(int b = 0; b < MaxBaskets; b++)
      if(g_baskets[b].active) c++;
   return c;
}

//+------------------------------------------------------------------+
//| Count positions for a magic number                                |
//+------------------------------------------------------------------+
int CountPositions(int magic)
{
   int c = 0;
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      c++;
   }
   return c;
}

//+------------------------------------------------------------------+
//| Close all positions of one basket                                 |
//+------------------------------------------------------------------+
int CloseBasketPositions(int magic)
{
   int closed = 0;
   trade.SetExpertMagicNumber(magic);

   int total = PositionsTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

      if(trade.PositionClose(ticket, Slippage))
         closed++;
   }
   return closed;
}

//+------------------------------------------------------------------+
//| Close ALL baskets                                                 |
//+------------------------------------------------------------------+
void CloseAllBaskets(string reason)
{
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;

      double pnl = GetBasketPnL(g_baskets[b].magic);
      int nClosed = CloseBasketPositions(g_baskets[b].magic);
      g_totalCycles++;
      g_totalProfit += pnl;
      g_totalTrades += nClosed;

      Print("B#", b, " CLOSED [", reason, "] $", DoubleToString(pnl, 2));
      ResetBasket(b);
   }
}

//+------------------------------------------------------------------+
//| Reset basket state                                                |
//+------------------------------------------------------------------+
void ResetBasket(int b)
{
   int magic = (b < ArraySize(g_baskets)) ? g_baskets[b].magic : BaseMagic + b + 1;

   g_baskets[b].magic         = magic;
   g_baskets[b].currentLevel  = 0;
   g_baskets[b].recoveryDir   = -1;
   g_baskets[b].lastBuyPrice  = 0;
   g_baskets[b].lastSellPrice = 0;
   g_baskets[b].entryMid      = 0;
   g_baskets[b].gridStep      = 0;
   g_baskets[b].active        = false;
   g_baskets[b].posCount      = 0;
   g_baskets[b].openTime      = 0;
}

//+------------------------------------------------------------------+
//| Recover baskets from existing positions after restart             |
//+------------------------------------------------------------------+
void RecoverAllBaskets()
{
   for(int b = 0; b < MaxBaskets; b++)
   {
      int magic = g_baskets[b].magic;
      int posCnt = CountPositions(magic);
      if(posCnt == 0) continue;

      double hiB = 0, loB = 999999, hiS = 0, loS = 999999;
      int nB = 0, nS = 0;
      double maxBV = 0, maxSV = 0, sumP = 0;

      int total = PositionsTotal();
      for(int i = 0; i < total; i++)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0) continue;
         if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

         double p = PositionGetDouble(POSITION_PRICE_OPEN);
         double v = PositionGetDouble(POSITION_VOLUME);
         int    t = (int)PositionGetInteger(POSITION_TYPE);
         sumP += p;

         if(t == POSITION_TYPE_BUY)
         {  nB++; hiB = MathMax(hiB, p); loB = MathMin(loB, p); maxBV = MathMax(maxBV, v); }
         else
         {  nS++; hiS = MathMax(hiS, p); loS = MathMin(loS, p); maxSV = MathMax(maxSV, v); }
      }

      if(maxBV > maxSV)
      {  g_baskets[b].recoveryDir = 0; g_baskets[b].lastBuyPrice = loB; g_baskets[b].lastSellPrice = loS; }
      else if(maxSV > maxBV)
      {  g_baskets[b].recoveryDir = 1; g_baskets[b].lastBuyPrice = hiB; g_baskets[b].lastSellPrice = hiS; }
      else
      {  g_baskets[b].recoveryDir = -1; g_baskets[b].lastBuyPrice = hiB; g_baskets[b].lastSellPrice = loS; }

      g_baskets[b].currentLevel = MathMax(0, (posCnt / 2) - 1);
      g_baskets[b].entryMid     = sumP / posCnt;
      g_baskets[b].gridStep     = GetEffectiveGridStep();
      g_baskets[b].active       = true;
      g_baskets[b].posCount     = posCnt;
      g_baskets[b].openTime     = TimeCurrent();
   }
}

//+------------------------------------------------------------------+
//| Dashboard — chart overlay (live mode only)                        |
//+------------------------------------------------------------------+
void CreateDashboard()
{
   string pfx = "HG_";
   int x = 10, y = 22, gap = 15;

   ObjectCreate(0, pfx+"bg", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_XDISTANCE, 5);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_YDISTANCE, 18);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_XSIZE, 320);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_YSIZE, 35 + (MaxBaskets + 4) * gap);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_BGCOLOR, C'15,15,25');
   ObjectSetInteger(0, pfx+"bg", OBJPROP_BORDER_COLOR, clrDodgerBlue);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_CORNER, CORNER_LEFT_UPPER);

   int nLabels = MaxBaskets + 5;
   for(int i = 0; i < nLabels; i++)
   {
      string name = pfx + "R" + IntegerToString(i);
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
      ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y + i * gap);
      ObjectSetInteger(0, name, OBJPROP_COLOR, clrWhite);
      ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
      ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetString(0, name, OBJPROP_FONT, "Consolas");
   }
}

void UpdateDashboard()
{
   if(MQLInfoInteger(MQL_TESTER)) return;

   string pfx = "HG_";
   int active = CountActive();
   int totalPos = 0;
   double floatPnL = 0;

   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;
      totalPos += CountPositions(g_baskets[b].magic);
      floatPnL += GetBasketPnL(g_baskets[b].magic);
   }

   double atr = GetATR();

   // Header
   ObjectSetString(0, pfx+"R0", OBJPROP_TEXT, "══ HEDGED GRID v3 ══");
   ObjectSetInteger(0, pfx+"R0", OBJPROP_COLOR, clrDodgerBlue);

   // ATR + grid info
   ObjectSetString(0, pfx+"R1", OBJPROP_TEXT,
      "ATR: " + DoubleToString(atr, 2) +
      "  Step: " + DoubleToString(UseATR ? atr * ATR_GridMult : GridStep, 2) +
      "  Dist: " + DoubleToString(UseATR ? atr * ATR_DistMult : MinDistance, 2));
   ObjectSetInteger(0, pfx+"R1", OBJPROP_COLOR, clrDarkGray);

   // Summary
   color pc = floatPnL >= 0 ? clrLime : clrOrangeRed;
   ObjectSetString(0, pfx+"R2", OBJPROP_TEXT,
      "Baskets: " + IntegerToString(active) + "/" + IntegerToString(MaxBaskets) +
      "  Pos: " + IntegerToString(totalPos) +
      "  Float: $" + DoubleToString(floatPnL, 2));
   ObjectSetInteger(0, pfx+"R2", OBJPROP_COLOR, pc);

   // Per basket
   for(int b = 0; b < MaxBaskets; b++)
   {
      string row;
      if(g_baskets[b].active)
      {
         string d = g_baskets[b].recoveryDir == 0 ? "B" :
                    g_baskets[b].recoveryDir == 1 ? "S" : "-";
         double bp = GetBasketPnL(g_baskets[b].magic);
         row = " #" + IntegerToString(b) +
               " Lv" + IntegerToString(g_baskets[b].currentLevel) +
               " " + d +
               " stp:" + DoubleToString(g_baskets[b].gridStep, 1) +
               " $" + DoubleToString(bp, 2);
      }
      else
         row = " #" + IntegerToString(b) + " [idle]";

      ObjectSetString(0, pfx+"R"+IntegerToString(3+b), OBJPROP_TEXT, row);
      ObjectSetInteger(0, pfx+"R"+IntegerToString(3+b), OBJPROP_COLOR,
         g_baskets[b].active ? clrWhite : clrDimGray);
   }

   // Totals
   int f1 = 3 + MaxBaskets;
   ObjectSetString(0, pfx+"R"+IntegerToString(f1), OBJPROP_TEXT,
      "Cycles: " + IntegerToString(g_totalCycles) +
      "  Trades: " + IntegerToString(g_totalTrades) +
      "  P/L: $" + DoubleToString(g_totalProfit, 2));
   ObjectSetInteger(0, pfx+"R"+IntegerToString(f1), OBJPROP_COLOR, clrGold);

   ObjectSetString(0, pfx+"R"+IntegerToString(f1+1), OBJPROP_TEXT,
      "DD: $" + DoubleToString(MaxDrawdown, 0) + "/b  $" +
      DoubleToString(MaxTotalDrawdown, 0) + "/tot");
   ObjectSetInteger(0, pfx+"R"+IntegerToString(f1+1), OBJPROP_COLOR, clrDimGray);

   ChartRedraw();
}
//+------------------------------------------------------------------+
