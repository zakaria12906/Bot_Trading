//+------------------------------------------------------------------+
//|                                                   HedgedGrid.mq5 |
//|               Hedged Grid Bot v4 — Trend-Biased Multi-Basket      |
//|                                                                    |
//|  KEY CHANGES from v3:                                              |
//|  1. TREND-BIASED OPENING: EMA detects trend, initial pair has     |
//|     directional bias (bigger lot on trend side). Correct trend    |
//|     = instant profit cycle. Wrong trend = normal grid recovery.   |
//|  2. FIXED GridStep (no ATR) — simpler, proven on Gold             |
//|  3. 3 baskets × 7 levels — more activity, controlled risk        |
//|  4. Lower BasketTP ($5) for faster cycling                        |
//|  5. Margin guard + drawdown safety                                |
//+------------------------------------------------------------------+

#property copyright "Hedged Grid Bot v4"
#property version   "4.00"
#property strict

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| INPUTS                                                            |
//+------------------------------------------------------------------+

// ── Lot sizing ──
input double   BaseLot          = 0.01;    // Base lot (hedge side)
input double   BiasMultiplier   = 2.0;     // Trend lot = BaseLot × this (e.g. 0.02)
input int      MaxLevels        = 7;       // Grid depth (lots up to 0.11)

// ── Grid spacing ──
input double   GridStep         = 3.5;     // Points between grid levels
input double   MinDistance      = 5.0;     // Min points between basket entries

// ── Take profit ──
input double   BasketTP         = 5.0;     // Close basket when net P/L >= this ($)

// ── Multi-basket ──
input int      MaxBaskets       = 3;       // Simultaneous baskets
input int      BaseMagic        = 888000;  // Base magic number

// ── Trend detection (EMA) ──
input int      EMA_Fast         = 10;      // Fast EMA period
input int      EMA_Slow         = 30;      // Slow EMA period
input ENUM_TIMEFRAMES EMA_TF   = PERIOD_M15; // EMA timeframe

// ── Session ──
input int      SessionStart     = 1;       // Start hour (server time)
input int      SessionEnd       = 23;      // End hour (server time)

// ── Risk ──
input double   MaxDrawdown      = 200.0;   // Emergency close ONE basket ($)
input double   MaxTotalDrawdown = 350.0;   // Emergency close ALL baskets ($)
input double   MinMarginPct     = 25.0;    // Stop if free margin < this %
input bool     CloseEndOfDay    = false;   // Force close at session end

// ── Execution ──
input int      Slippage         = 30;      // Max slippage points

//+------------------------------------------------------------------+
//| BASKET STATE                                                      |
//+------------------------------------------------------------------+
struct SBasket
{
   int      magic;
   int      currentLevel;
   int      recoveryDir;      // -1=none, 0=BUY, 1=SELL
   int      trendBias;        // 0=BUY bias, 1=SELL bias, -1=neutral
   double   lastBuyPrice;
   double   lastSellPrice;
   double   entryMid;
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
int      g_emaFastH = INVALID_HANDLE;
int      g_emaSlowH = INVALID_HANDLE;
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

   // Lot sequence
   double mults[9] = {1, 1, 2, 3, 5, 7, 11, 17, 25};
   for(int i = 0; i < 9; i++)
   {
      LotSeq[i] = NormalizeDouble(BaseLot * mults[i], 2);
      LotSeq[i] = MathMax(LotSeq[i], SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));
   }

   // EMA indicators
   g_emaFastH = iMA(_Symbol, EMA_TF, EMA_Fast, 0, MODE_EMA, PRICE_CLOSE);
   g_emaSlowH = iMA(_Symbol, EMA_TF, EMA_Slow, 0, MODE_EMA, PRICE_CLOSE);

   if(g_emaFastH == INVALID_HANDLE || g_emaSlowH == INVALID_HANDLE)
      Print("WARNING: EMA handles failed — opening without bias");

   // Baskets
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
   { if(i > 0) lots += ","; lots += DoubleToString(LotSeq[i], 2); }

   double biasLot = NormalizeDouble(BaseLot * BiasMultiplier, 2);
   Print("=== HEDGED GRID v4 — TREND BIASED ===");
   Print(_Symbol, " | Step: ", GridStep, " | TP: $", BasketTP,
         " | Baskets: ", MaxBaskets);
   Print("Base: ", DoubleToString(BaseLot, 2),
         " | Bias lot: ", DoubleToString(biasLot, 2),
         " (×", DoubleToString(BiasMultiplier, 1), ")");
   Print("EMA(", EMA_Fast, "/", EMA_Slow, " on ", EnumToString(EMA_TF), ")");
   Print("Recovery lots: [", lots, "] | Levels: ", MaxLevels);
   Print("DD: $", MaxDrawdown, "/basket | $", MaxTotalDrawdown, "/total");

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
   if(g_emaFastH != INVALID_HANDLE) IndicatorRelease(g_emaFastH);
   if(g_emaSlowH != INVALID_HANDLE) IndicatorRelease(g_emaSlowH);

   Print("=== v4 STOPPED | Cycles: ", g_totalCycles,
         " | P/L: $", DoubleToString(g_totalProfit, 2),
         " | Trades: ", g_totalTrades, " ===");
}

//+------------------------------------------------------------------+
//| Detect trend bias: 0=BUY, 1=SELL, -1=neutral                    |
//+------------------------------------------------------------------+
int DetectTrend()
{
   if(g_emaFastH == INVALID_HANDLE || g_emaSlowH == INVALID_HANDLE)
      return -1;

   double fast[2], slow[2];
   if(CopyBuffer(g_emaFastH, 0, 0, 2, fast) < 2) return -1;
   if(CopyBuffer(g_emaSlowH, 0, 0, 2, slow) < 2) return -1;

   // Current: fast vs slow
   double diff = fast[1] - slow[1];
   // Trend strength: how fast the EMA gap is changing
   double prevDiff = fast[0] - slow[0];
   double momentum = diff - prevDiff;

   // Require both crossover AND momentum confirmation
   if(diff > 0 && momentum >= 0) return 0;  // BUY trend
   if(diff < 0 && momentum <= 0) return 1;  // SELL trend

   return -1;  // neutral / conflicting
}

//+------------------------------------------------------------------+
//| Margin guard                                                      |
//+------------------------------------------------------------------+
bool HasEnoughMargin(double lot)
{
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);

   if(equity <= 0) return false;
   if((freeMargin / equity) * 100.0 < MinMarginPct) return false;

   double marginReq = 0;
   if(OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lot,
                       SymbolInfoDouble(_Symbol, SYMBOL_ASK), marginReq))
   {
      if(marginReq > freeMargin * 0.5) return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| OnTick                                                            |
//+------------------------------------------------------------------+
void OnTick()
{
   MqlDateTime dt;
   TimeCurrent(dt);
   bool inSession = (dt.hour >= SessionStart && dt.hour < SessionEnd);

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(bid == 0 || ask == 0) return;

   // Global drawdown safety
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

   // Session end close
   if(CloseEndOfDay && !inSession && CountActive() > 0)
   { CloseAllBaskets("SESSION_END"); return; }

   // Process each active basket
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;

      double pnl = GetBasketPnL(g_baskets[b].magic);

      // Take profit
      if(pnl >= BasketTP)
      {
         int nc = CloseBasketPositions(g_baskets[b].magic);
         g_totalCycles++;
         g_totalProfit += pnl;
         g_totalTrades += nc;

         string dir = g_baskets[b].trendBias == 0 ? "BULL" :
                      g_baskets[b].trendBias == 1 ? "BEAR" : "NEUT";
         Print("B#", b, " TP | Lv", g_baskets[b].currentLevel,
               " ", dir, " | ", nc, " pos | $", DoubleToString(pnl, 2),
               " | #", g_totalCycles,
               " | Tot: $", DoubleToString(g_totalProfit, 2));

         ResetBasket(b);
         continue;
      }

      // Per-basket drawdown
      if(MaxDrawdown > 0 && pnl <= -MaxDrawdown)
      {
         int nc = CloseBasketPositions(g_baskets[b].magic);
         g_totalCycles++;
         g_totalProfit += pnl;
         g_totalTrades += nc;
         Print("B#", b, " DD STOP | $", DoubleToString(pnl, 2));
         ResetBasket(b);
         continue;
      }

      // Check next grid level
      if(g_baskets[b].currentLevel < MaxLevels - 1)
         CheckNextLevel(b, bid, ask);
   }

   // Open new baskets if slots available
   if(inSession)
   {
      int active = CountActive();
      while(active < MaxBaskets)
      {
         if(!TryOpenNewBasket(bid, ask)) break;
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
   double biasLot = NormalizeDouble(BaseLot * BiasMultiplier, 2);
   biasLot = MathMax(biasLot, SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));

   if(!HasEnoughMargin(biasLot + BaseLot))
      return false;

   double mid = (bid + ask) / 2.0;

   // Distance check from existing baskets
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;
      if(MathAbs(mid - g_baskets[b].entryMid) < MinDistance)
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
//| Open basket with TREND BIAS                                       |
//+------------------------------------------------------------------+
void OpenBasket(int b, double bid, double ask)
{
   int trend = DetectTrend();  // 0=BUY, 1=SELL, -1=neutral

   double biasLot = NormalizeDouble(BaseLot * BiasMultiplier, 2);
   biasLot = MathMax(biasLot, SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));

   // Determine lots based on trend
   double buyLot, sellLot;
   if(trend == 0)       { buyLot = biasLot; sellLot = BaseLot; }   // Bullish
   else if(trend == 1)  { buyLot = BaseLot; sellLot = biasLot; }   // Bearish
   else                 { buyLot = BaseLot; sellLot = BaseLot; }   // Neutral

   int magic = g_baskets[b].magic;
   trade.SetExpertMagicNumber(magic);

   // BUY
   if(!trade.Buy(buyLot, _Symbol, ask, 0, 0,
                  "HG_B" + IntegerToString(b) + "_L0_BUY"))
   {
      Print("B#", b, " BUY failed: ", trade.ResultRetcodeDescription());
      return;
   }

   // SELL
   if(!trade.Sell(sellLot, _Symbol, bid, 0, 0,
                   "HG_B" + IntegerToString(b) + "_L0_SELL"))
   {
      Print("B#", b, " SELL failed — closing BUY");
      CloseBasketPositions(magic);
      return;
   }

   g_baskets[b].currentLevel  = 0;
   g_baskets[b].recoveryDir   = -1;
   g_baskets[b].trendBias     = trend;
   g_baskets[b].lastBuyPrice  = ask;
   g_baskets[b].lastSellPrice = bid;
   g_baskets[b].entryMid      = (bid + ask) / 2.0;
   g_baskets[b].active        = true;
   g_baskets[b].posCount      = 2;
   g_baskets[b].openTime      = TimeCurrent();

   string dir = trend == 0 ? "BULL" : trend == 1 ? "BEAR" : "NEUT";
   Print("B#", b, " OPENED [", dir, "] BUY=",
         DoubleToString(buyLot, 2), " SELL=",
         DoubleToString(sellLot, 2),
         " @ ", DoubleToString((bid+ask)/2.0, _Digits),
         " | Active: ", CountActive());
}

//+------------------------------------------------------------------+
//| Check next grid level                                             |
//+------------------------------------------------------------------+
void CheckNextLevel(int b, double bid, double ask)
{
   double buyTrig  = g_baskets[b].lastBuyPrice  - GridStep;
   double sellTrig = g_baskets[b].lastSellPrice + GridStep;
   int    recDir   = g_baskets[b].recoveryDir;

   if(recDir == -1)
   {
      if(bid <= buyTrig && ask >= sellTrig)
      {
         if((g_baskets[b].lastBuyPrice - bid) >= (ask - g_baskets[b].lastSellPrice))
            AddLevel(b, 0, bid, ask);
         else
            AddLevel(b, 1, bid, ask);
      }
      else if(bid <= buyTrig)  AddLevel(b, 0, bid, ask);
      else if(ask >= sellTrig) AddLevel(b, 1, bid, ask);
   }
   else if(recDir == 0 && bid <= buyTrig)  AddLevel(b, 0, bid, ask);
   else if(recDir == 1 && ask >= sellTrig) AddLevel(b, 1, bid, ask);
}

//+------------------------------------------------------------------+
//| Add grid level                                                    |
//+------------------------------------------------------------------+
void AddLevel(int b, int recovDir, double bid, double ask)
{
   int nextLv = g_baskets[b].currentLevel + 1;
   if(nextLv >= MaxLevels) return;

   double nextLot = GetLot(nextLv);
   if(!HasEnoughMargin(nextLot + BaseLot)) return;

   if(g_baskets[b].recoveryDir == -1)
      g_baskets[b].recoveryDir = recovDir;

   int magic = g_baskets[b].magic;
   trade.SetExpertMagicNumber(magic);

   double recLot = (g_baskets[b].recoveryDir == recovDir) ? nextLot : BaseLot;
   string tag = "HG_B" + IntegerToString(b) + "_L" + IntegerToString(nextLv);

   if(recovDir == 0)
   {
      if(!trade.Buy(recLot, _Symbol, ask, 0, 0, tag + "_BUY"))
      { Print("B#", b, " L", nextLv, " BUY fail"); return; }
      g_baskets[b].lastBuyPrice = ask;
      g_baskets[b].posCount++;

      if(trade.Sell(BaseLot, _Symbol, bid, 0, 0, tag + "_SELL"))
      { g_baskets[b].lastSellPrice = bid; g_baskets[b].posCount++; }
   }
   else
   {
      if(!trade.Sell(recLot, _Symbol, bid, 0, 0, tag + "_SELL"))
      { Print("B#", b, " L", nextLv, " SELL fail"); return; }
      g_baskets[b].lastSellPrice = bid;
      g_baskets[b].posCount++;

      if(trade.Buy(BaseLot, _Symbol, ask, 0, 0, tag + "_BUY"))
      { g_baskets[b].lastBuyPrice = ask; g_baskets[b].posCount++; }
   }

   g_baskets[b].currentLevel = nextLv;

   string dir = recovDir == 0 ? "BUY" : "SELL";
   Print("B#", b, " Lv", nextLv, " ", dir, " rec | ",
         DoubleToString(recLot, 2), "+", DoubleToString(BaseLot, 2),
         " | $", DoubleToString(GetBasketPnL(magic), 2));
}

//+------------------------------------------------------------------+
//| Get lot from sequence                                             |
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
//| P/L for one basket                                                |
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
//| Total P/L all baskets                                             |
//+------------------------------------------------------------------+
double GetTotalPnL()
{
   double t = 0;
   for(int b = 0; b < MaxBaskets; b++)
      if(g_baskets[b].active) t += GetBasketPnL(g_baskets[b].magic);
   return t;
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
//| Count positions for magic                                         |
//+------------------------------------------------------------------+
int CountPositions(int magic)
{
   int c = 0, t = PositionsTotal();
   for(int i = 0; i < t; i++)
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
//| Close all positions of a basket                                   |
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
      if(trade.PositionClose(ticket, Slippage)) closed++;
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
      int nc = CloseBasketPositions(g_baskets[b].magic);
      g_totalCycles++;
      g_totalProfit += pnl;
      g_totalTrades += nc;
      Print("B#", b, " [", reason, "] $", DoubleToString(pnl, 2));
      ResetBasket(b);
   }
}

//+------------------------------------------------------------------+
//| Reset basket                                                      |
//+------------------------------------------------------------------+
void ResetBasket(int b)
{
   int magic = (b < ArraySize(g_baskets)) ? g_baskets[b].magic : BaseMagic + b + 1;
   g_baskets[b].magic         = magic;
   g_baskets[b].currentLevel  = 0;
   g_baskets[b].recoveryDir   = -1;
   g_baskets[b].trendBias     = -1;
   g_baskets[b].lastBuyPrice  = 0;
   g_baskets[b].lastSellPrice = 0;
   g_baskets[b].entryMid      = 0;
   g_baskets[b].active        = false;
   g_baskets[b].posCount      = 0;
   g_baskets[b].openTime      = 0;
}

//+------------------------------------------------------------------+
//| Recover baskets after restart                                     |
//+------------------------------------------------------------------+
void RecoverAllBaskets()
{
   for(int b = 0; b < MaxBaskets; b++)
   {
      int magic = g_baskets[b].magic;
      int posCnt = CountPositions(magic);
      if(posCnt == 0) continue;

      double hiB=0, loB=999999, hiS=0, loS=999999;
      int nB=0, nS=0;
      double maxBV=0, maxSV=0, sumP=0;

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
         { nB++; hiB=MathMax(hiB,p); loB=MathMin(loB,p); maxBV=MathMax(maxBV,v); }
         else
         { nS++; hiS=MathMax(hiS,p); loS=MathMin(loS,p); maxSV=MathMax(maxSV,v); }
      }

      if(maxBV > maxSV)
      { g_baskets[b].recoveryDir=0; g_baskets[b].lastBuyPrice=loB; g_baskets[b].lastSellPrice=loS; }
      else if(maxSV > maxBV)
      { g_baskets[b].recoveryDir=1; g_baskets[b].lastBuyPrice=hiB; g_baskets[b].lastSellPrice=hiS; }
      else
      { g_baskets[b].recoveryDir=-1; g_baskets[b].lastBuyPrice=hiB; g_baskets[b].lastSellPrice=loS; }

      g_baskets[b].currentLevel = MathMax(0, (posCnt/2) - 1);
      g_baskets[b].entryMid     = sumP / posCnt;
      g_baskets[b].trendBias    = (maxBV > maxSV) ? 0 : (maxSV > maxBV) ? 1 : -1;
      g_baskets[b].active       = true;
      g_baskets[b].posCount     = posCnt;
      g_baskets[b].openTime     = TimeCurrent();
   }
}

//+------------------------------------------------------------------+
//| Dashboard                                                         |
//+------------------------------------------------------------------+
void CreateDashboard()
{
   string pfx = "HG_";
   ObjectCreate(0, pfx+"bg", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_XDISTANCE, 5);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_YDISTANCE, 18);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_XSIZE, 330);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_YSIZE, 30 + (MaxBaskets + 5) * 15);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_BGCOLOR, C'15,15,25');
   ObjectSetInteger(0, pfx+"bg", OBJPROP_BORDER_COLOR, clrDodgerBlue);
   ObjectSetInteger(0, pfx+"bg", OBJPROP_CORNER, CORNER_LEFT_UPPER);

   for(int i = 0; i < MaxBaskets + 6; i++)
   {
      string name = pfx + "R" + IntegerToString(i);
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_XDISTANCE, 10);
      ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 22 + i * 15);
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

   int trend = DetectTrend();
   string trendStr = trend == 0 ? "BULL" : trend == 1 ? "BEAR" : "FLAT";

   ObjectSetString(0, pfx+"R0", OBJPROP_TEXT, "══ HEDGED GRID v4 ══");
   ObjectSetInteger(0, pfx+"R0", OBJPROP_COLOR, clrDodgerBlue);

   ObjectSetString(0, pfx+"R1", OBJPROP_TEXT,
      "Trend: " + trendStr + "  Step: " + DoubleToString(GridStep, 1));
   ObjectSetInteger(0, pfx+"R1", OBJPROP_COLOR,
      trend == 0 ? clrLime : trend == 1 ? clrOrangeRed : clrGray);

   color pc = floatPnL >= 0 ? clrLime : clrOrangeRed;
   ObjectSetString(0, pfx+"R2", OBJPROP_TEXT,
      "Active: " + IntegerToString(active) + "/" + IntegerToString(MaxBaskets) +
      "  Pos: " + IntegerToString(totalPos) +
      "  $" + DoubleToString(floatPnL, 2));
   ObjectSetInteger(0, pfx+"R2", OBJPROP_COLOR, pc);

   for(int b = 0; b < MaxBaskets; b++)
   {
      string row;
      if(g_baskets[b].active)
      {
         string d = g_baskets[b].recoveryDir == 0 ? "Brec" :
                    g_baskets[b].recoveryDir == 1 ? "Srec" : "---";
         string tb = g_baskets[b].trendBias == 0 ? "BU" :
                     g_baskets[b].trendBias == 1 ? "BE" : "N";
         double bp = GetBasketPnL(g_baskets[b].magic);
         row = " #" + IntegerToString(b) + " " + tb +
               " Lv" + IntegerToString(g_baskets[b].currentLevel) +
               " " + d + " $" + DoubleToString(bp, 2);
      }
      else row = " #" + IntegerToString(b) + " [idle]";

      ObjectSetString(0, pfx+"R"+IntegerToString(3+b), OBJPROP_TEXT, row);
      ObjectSetInteger(0, pfx+"R"+IntegerToString(3+b), OBJPROP_COLOR,
         g_baskets[b].active ? clrWhite : clrDimGray);
   }

   int f = 3 + MaxBaskets;
   ObjectSetString(0, pfx+"R"+IntegerToString(f), OBJPROP_TEXT,
      "Cycles: " + IntegerToString(g_totalCycles) +
      " Trades: " + IntegerToString(g_totalTrades) +
      " P/L: $" + DoubleToString(g_totalProfit, 2));
   ObjectSetInteger(0, pfx+"R"+IntegerToString(f), OBJPROP_COLOR, clrGold);

   ObjectSetString(0, pfx+"R"+IntegerToString(f+1), OBJPROP_TEXT,
      "DD: $" + DoubleToString(MaxDrawdown, 0) + "/b  $" +
      DoubleToString(MaxTotalDrawdown, 0) + "/tot  Margin: " +
      DoubleToString(MinMarginPct, 0) + "%");
   ObjectSetInteger(0, pfx+"R"+IntegerToString(f+1), OBJPROP_COLOR, clrDimGray);

   ChartRedraw();
}
//+------------------------------------------------------------------+
