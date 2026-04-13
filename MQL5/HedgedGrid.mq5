//+------------------------------------------------------------------+
//|                                                   HedgedGrid.mq5 |
//|                    Hedged Grid Bot v2 — Multi-Basket EA           |
//|                                                                    |
//|  MAJOR UPGRADE: Multiple simultaneous baskets for maximum         |
//|  position density and profit, matching the observed live bot.     |
//|                                                                    |
//|  LOGIC:                                                            |
//|  - Run up to MaxBaskets independent grids at the same time        |
//|  - Each basket: BUY+SELL hedge pair → grid levels → basket close  |
//|  - New basket opens when price moves MinDistance from existing     |
//|  - Tight GridStep + low BasketTP = fast cycling = many positions  |
//|  - Result: 30-80+ positions/day like the observed live bot        |
//|                                                                    |
//|  Lot sequence: 0.01, 0.01, 0.02, 0.03, 0.05, 0.07, 0.11,       |
//|                0.17, 0.25                                          |
//+------------------------------------------------------------------+

#property copyright "Hedged Grid Bot v2"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| INPUT PARAMETERS                                                  |
//+------------------------------------------------------------------+
input double   BaseLot          = 0.01;    // Base lot size
input int      MaxLevels        = 9;       // Maximum grid depth per basket
input double   GridStep         = 3.0;     // Points between grid levels
input double   BasketTP         = 8.0;     // Close basket when net P/L >= this ($)
input int      MaxBaskets       = 3;       // Max simultaneous baskets
input double   MinDistance      = 8.0;     // Min points between basket entries
input int      BaseMagic        = 888000;  // Base magic number (each basket adds +1)
input int      SessionStart     = 1;       // Session start hour (UTC)
input int      SessionEnd       = 23;      // Session end hour (UTC)
input double   MaxDrawdown      = 300.0;   // Emergency close basket if loss > this ($)
input double   MaxTotalDrawdown = 800.0;   // Emergency close ALL if total loss > this ($)
input bool     CloseEndOfDay    = false;   // Force close all baskets at SessionEnd
input int      Slippage         = 30;      // Max slippage in points

//+------------------------------------------------------------------+
//| BASKET STATE STRUCTURE                                            |
//+------------------------------------------------------------------+
struct SBasket
{
   int    magic;
   int    currentLevel;
   int    recoveryDir;      // -1=none, 0=BUY, 1=SELL
   double lastBuyPrice;
   double lastSellPrice;
   double entryMid;         // mid price at basket open (for distance check)
   bool   active;
   int    positionCount;
   datetime openTime;
};

//+------------------------------------------------------------------+
//| GLOBAL VARIABLES                                                  |
//+------------------------------------------------------------------+
CTrade trade;

double LotSequence[9] = {0.01, 0.01, 0.02, 0.03, 0.05, 0.07, 0.11, 0.17, 0.25};

SBasket g_baskets[];
int     g_totalCycles  = 0;
double  g_totalProfit  = 0.0;
int     g_totalTrades  = 0;

//+------------------------------------------------------------------+
//| Expert initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(BaseMagic);
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   // Build lot sequence if BaseLot != 0.01
   if(BaseLot != 0.01)
   {
      double mults[9] = {1, 1, 2, 3, 5, 7, 11, 17, 25};
      for(int i = 0; i < 9; i++)
      {
         LotSequence[i] = NormalizeDouble(BaseLot * mults[i], 2);
         LotSequence[i] = MathMax(LotSequence[i], SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));
      }
   }

   // Initialize basket array
   ArrayResize(g_baskets, MaxBaskets);
   for(int b = 0; b < MaxBaskets; b++)
   {
      g_baskets[b].magic        = BaseMagic + b + 1;
      g_baskets[b].currentLevel = 0;
      g_baskets[b].recoveryDir  = -1;
      g_baskets[b].lastBuyPrice = 0;
      g_baskets[b].lastSellPrice= 0;
      g_baskets[b].entryMid     = 0;
      g_baskets[b].active       = false;
      g_baskets[b].positionCount= 0;
      g_baskets[b].openTime     = 0;
   }

   // Recover any existing baskets from open positions
   RecoverAllBaskets();

   // Log startup
   string lots = "";
   for(int i = 0; i < MathMin(MaxLevels, 9); i++)
   {
      if(i > 0) lots += ", ";
      lots += DoubleToString(LotSequence[i], 2);
   }
   Print("=== HEDGED GRID BOT v2 STARTED ===");
   Print("Symbol: ", _Symbol, " | Step: ", GridStep, " | TP: $", BasketTP);
   Print("Max baskets: ", MaxBaskets, " | Min distance: ", MinDistance);
   Print("Max levels: ", MaxLevels, " | Lots: [", lots, "]");
   Print("Session: ", SessionStart, "-", SessionEnd, " UTC");
   Print("DD per basket: $", MaxDrawdown, " | DD total: $", MaxTotalDrawdown);

   int activeCount = CountActiveBaskets();
   if(activeCount > 0)
      Print("Recovered ", activeCount, " active baskets");

   if(!MQLInfoInteger(MQL_TESTER))
      CreateDashboard();

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("=== HEDGED GRID BOT v2 STOPPED ===");
   Print("Total cycles: ", g_totalCycles, " | Total profit: $",
         DoubleToString(g_totalProfit, 2), " | Total trades: ", g_totalTrades);
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
   MqlDateTime dt;
   TimeCurrent(dt);
   bool inSession = (dt.hour >= SessionStart && dt.hour < SessionEnd);

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(bid == 0 || ask == 0) return;

   // GLOBAL SAFETY: total drawdown across all baskets
   if(MaxTotalDrawdown > 0)
   {
      double totalPnL = GetTotalPnL();
      if(totalPnL <= -MaxTotalDrawdown)
      {
         CloseAllBaskets("TOTAL_DD_STOP");
         Print("!!! TOTAL DRAWDOWN STOP: $", DoubleToString(totalPnL, 2));
         return;
      }
   }

   // SESSION END: force close all if configured
   if(CloseEndOfDay && !inSession)
   {
      int active = CountActiveBaskets();
      if(active > 0)
      {
         CloseAllBaskets("SESSION_END");
         Print("Session ended — closed ", active, " baskets");
      }
      return;
   }

   // Process each basket
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;

      double pnl = GetBasketPnL(g_baskets[b].magic);

      // TAKE PROFIT
      if(pnl >= BasketTP)
      {
         int closed = CloseBasket(b, "TP");
         g_totalCycles++;
         g_totalProfit += pnl;
         g_totalTrades += closed;

         Print("BASKET #", b, " CLOSED [TP] | Level: ", g_baskets[b].currentLevel,
               " | Positions: ", closed,
               " | P/L: $", DoubleToString(pnl, 2),
               " | Cycle #", g_totalCycles,
               " | Day total: $", DoubleToString(g_totalProfit, 2));
         continue;
      }

      // MAX DRAWDOWN per basket
      if(MaxDrawdown > 0 && pnl <= -MaxDrawdown)
      {
         int closed = CloseBasket(b, "DD_STOP");
         g_totalCycles++;
         g_totalProfit += pnl;
         g_totalTrades += closed;

         Print("BASKET #", b, " CLOSED [DD STOP] | P/L: $",
               DoubleToString(pnl, 2));
         continue;
      }

      // CHECK NEXT GRID LEVEL
      if(g_baskets[b].currentLevel < MaxLevels - 1)
         CheckNextLevel(b, bid, ask);
   }

   // OPEN NEW BASKETS if capacity available and in session
   if(inSession)
   {
      int active = CountActiveBaskets();
      if(active < MaxBaskets)
         TryOpenNewBasket(bid, ask);
   }

   // Update dashboard
   UpdateDashboard();
}

//+------------------------------------------------------------------+
//| Try to open a new basket (check distance from existing ones)      |
//+------------------------------------------------------------------+
void TryOpenNewBasket(double bid, double ask)
{
   double mid = (bid + ask) / 2.0;

   // Check minimum distance from all active baskets
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;
      if(MathAbs(mid - g_baskets[b].entryMid) < MinDistance)
         return;  // Too close to an existing basket
   }

   // Find first free slot
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active)
      {
         OpenBasket(b, bid, ask);
         return;
      }
   }
}

//+------------------------------------------------------------------+
//| Open a new basket (initial BUY + SELL pair)                       |
//+------------------------------------------------------------------+
void OpenBasket(int b, double bid, double ask)
{
   int magic = g_baskets[b].magic;
   trade.SetExpertMagicNumber(magic);

   // BUY
   if(!trade.Buy(BaseLot, _Symbol, ask, 0, 0,
                  "HG_B" + IntegerToString(b) + "_L0_BUY"))
   {
      Print("Basket #", b, " initial BUY failed: ", trade.ResultRetcodeDescription());
      return;
   }

   // SELL
   if(!trade.Sell(BaseLot, _Symbol, bid, 0, 0,
                   "HG_B" + IntegerToString(b) + "_L0_SELL"))
   {
      Print("Basket #", b, " initial SELL failed — closing BUY");
      CloseBasketPositions(magic);
      return;
   }

   g_baskets[b].currentLevel  = 0;
   g_baskets[b].recoveryDir   = -1;
   g_baskets[b].lastBuyPrice  = ask;
   g_baskets[b].lastSellPrice = bid;
   g_baskets[b].entryMid      = (bid + ask) / 2.0;
   g_baskets[b].active        = true;
   g_baskets[b].positionCount = 2;
   g_baskets[b].openTime      = TimeCurrent();

   Print("BASKET #", b, " OPENED | BUY+SELL @ ",
         DoubleToString((bid + ask) / 2.0, _Digits),
         " | Active baskets: ", CountActiveBaskets());
}

//+------------------------------------------------------------------+
//| Check if next grid level should trigger for a specific basket     |
//+------------------------------------------------------------------+
void CheckNextLevel(int b, double bid, double ask)
{
   double buyTrigger  = g_baskets[b].lastBuyPrice - GridStep;
   double sellTrigger = g_baskets[b].lastSellPrice + GridStep;
   int    recDir      = g_baskets[b].recoveryDir;

   if(recDir == -1)
   {
      // Recovery direction not set yet
      if(bid <= buyTrigger && ask >= sellTrigger)
      {
         if((g_baskets[b].lastBuyPrice - bid) >= (ask - g_baskets[b].lastSellPrice))
            AddLevelBuyRecovery(b, bid, ask);
         else
            AddLevelSellRecovery(b, bid, ask);
      }
      else if(bid <= buyTrigger)
         AddLevelBuyRecovery(b, bid, ask);
      else if(ask >= sellTrigger)
         AddLevelSellRecovery(b, bid, ask);
   }
   else if(recDir == 0)  // BUY is recovery → price must drop
   {
      if(bid <= buyTrigger)
         AddLevelBuyRecovery(b, bid, ask);
   }
   else if(recDir == 1)  // SELL is recovery → price must rise
   {
      if(ask >= sellTrigger)
         AddLevelSellRecovery(b, bid, ask);
   }
}

//+------------------------------------------------------------------+
//| Add BUY recovery level (price dropped)                            |
//+------------------------------------------------------------------+
void AddLevelBuyRecovery(int b, double bid, double ask)
{
   int nextLevel = g_baskets[b].currentLevel + 1;
   if(nextLevel >= MaxLevels) return;

   if(g_baskets[b].recoveryDir == -1)
      g_baskets[b].recoveryDir = 0;

   double recoveryLot = (g_baskets[b].recoveryDir == 0) ? GetLot(nextLevel) : BaseLot;
   int magic = g_baskets[b].magic;
   trade.SetExpertMagicNumber(magic);

   // Recovery BUY (bigger lot)
   if(!trade.Buy(recoveryLot, _Symbol, ask, 0, 0,
                  "HG_B" + IntegerToString(b) + "_L" + IntegerToString(nextLevel) + "_BUY"))
   {
      Print("Basket #", b, " level ", nextLevel, " BUY failed");
      return;
   }
   g_baskets[b].lastBuyPrice = ask;
   g_baskets[b].positionCount++;

   // Hedge SELL (base lot)
   if(trade.Sell(BaseLot, _Symbol, bid, 0, 0,
                  "HG_B" + IntegerToString(b) + "_L" + IntegerToString(nextLevel) + "_SELL"))
   {
      g_baskets[b].lastSellPrice = bid;
      g_baskets[b].positionCount++;
   }

   g_baskets[b].currentLevel = nextLevel;

   Print("B#", b, " LEVEL ", nextLevel, " (BUY rec) | BUY ",
         DoubleToString(recoveryLot, 2), " + SELL ",
         DoubleToString(BaseLot, 2),
         " | Net: $", DoubleToString(GetBasketPnL(magic), 2));
}

//+------------------------------------------------------------------+
//| Add SELL recovery level (price rose)                              |
//+------------------------------------------------------------------+
void AddLevelSellRecovery(int b, double bid, double ask)
{
   int nextLevel = g_baskets[b].currentLevel + 1;
   if(nextLevel >= MaxLevels) return;

   if(g_baskets[b].recoveryDir == -1)
      g_baskets[b].recoveryDir = 1;

   double recoveryLot = (g_baskets[b].recoveryDir == 1) ? GetLot(nextLevel) : BaseLot;
   int magic = g_baskets[b].magic;
   trade.SetExpertMagicNumber(magic);

   // Recovery SELL (bigger lot)
   if(!trade.Sell(recoveryLot, _Symbol, bid, 0, 0,
                   "HG_B" + IntegerToString(b) + "_L" + IntegerToString(nextLevel) + "_SELL"))
   {
      Print("Basket #", b, " level ", nextLevel, " SELL failed");
      return;
   }
   g_baskets[b].lastSellPrice = bid;
   g_baskets[b].positionCount++;

   // Hedge BUY (base lot)
   if(trade.Buy(BaseLot, _Symbol, ask, 0, 0,
                 "HG_B" + IntegerToString(b) + "_L" + IntegerToString(nextLevel) + "_BUY"))
   {
      g_baskets[b].lastBuyPrice = ask;
      g_baskets[b].positionCount++;
   }

   g_baskets[b].currentLevel = nextLevel;

   Print("B#", b, " LEVEL ", nextLevel, " (SELL rec) | SELL ",
         DoubleToString(recoveryLot, 2), " + BUY ",
         DoubleToString(BaseLot, 2),
         " | Net: $", DoubleToString(GetBasketPnL(magic), 2));
}

//+------------------------------------------------------------------+
//| Get lot size from sequence                                        |
//+------------------------------------------------------------------+
double GetLot(int level)
{
   if(level < 0 || level >= 9) return BaseLot;

   double lot = LotSequence[level];
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   lot = MathMax(lot, minLot);
   lot = MathMin(lot, maxLot);
   lot = MathFloor(lot / lotStep) * lotStep;
   return NormalizeDouble(lot, 2);
}

//+------------------------------------------------------------------+
//| Get net P/L for a specific basket (by magic number)               |
//+------------------------------------------------------------------+
double GetBasketPnL(int magic)
{
   double total = 0.0;
   int count = PositionsTotal();

   for(int i = 0; i < count; i++)
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
//| Get total P/L across ALL baskets                                  |
//+------------------------------------------------------------------+
double GetTotalPnL()
{
   double total = 0.0;
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(g_baskets[b].active)
         total += GetBasketPnL(g_baskets[b].magic);
   }
   return total;
}

//+------------------------------------------------------------------+
//| Count active baskets                                              |
//+------------------------------------------------------------------+
int CountActiveBaskets()
{
   int count = 0;
   for(int b = 0; b < MaxBaskets; b++)
      if(g_baskets[b].active) count++;
   return count;
}

//+------------------------------------------------------------------+
//| Count positions for a specific magic number                       |
//+------------------------------------------------------------------+
int CountPositions(int magic)
{
   int count = 0;
   int total = PositionsTotal();

   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      count++;
   }
   return count;
}

//+------------------------------------------------------------------+
//| Close a single basket and reset its state                         |
//+------------------------------------------------------------------+
int CloseBasket(int b, string reason)
{
   int closed = CloseBasketPositions(g_baskets[b].magic);

   g_baskets[b].currentLevel  = 0;
   g_baskets[b].recoveryDir   = -1;
   g_baskets[b].lastBuyPrice  = 0;
   g_baskets[b].lastSellPrice = 0;
   g_baskets[b].entryMid      = 0;
   g_baskets[b].active        = false;
   g_baskets[b].positionCount = 0;
   g_baskets[b].openTime      = 0;

   return closed;
}

//+------------------------------------------------------------------+
//| Close all positions with a specific magic number                  |
//+------------------------------------------------------------------+
int CloseBasketPositions(int magic)
{
   int closed = 0;
   int total = PositionsTotal();

   for(int i = total - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

      trade.SetExpertMagicNumber(magic);
      if(trade.PositionClose(ticket, Slippage))
         closed++;
      else
         Print("Failed to close ticket ", ticket);
   }
   return closed;
}

//+------------------------------------------------------------------+
//| Close ALL baskets (emergency or session end)                      |
//+------------------------------------------------------------------+
void CloseAllBaskets(string reason)
{
   for(int b = 0; b < MaxBaskets; b++)
   {
      if(!g_baskets[b].active) continue;

      double pnl = GetBasketPnL(g_baskets[b].magic);
      int closed = CloseBasket(b, reason);
      g_totalCycles++;
      g_totalProfit += pnl;
      g_totalTrades += closed;

      Print("BASKET #", b, " CLOSED [", reason, "] | P/L: $",
            DoubleToString(pnl, 2));
   }
}

//+------------------------------------------------------------------+
//| Recover all basket states from existing positions after restart   |
//+------------------------------------------------------------------+
void RecoverAllBaskets()
{
   for(int b = 0; b < MaxBaskets; b++)
   {
      int magic = g_baskets[b].magic;
      int posCount = CountPositions(magic);

      if(posCount == 0) continue;

      double highestBuy = 0, lowestBuy = 999999;
      double highestSell = 0, lowestSell = 999999;
      int buyCount = 0, sellCount = 0;
      double maxBuyLot = 0, maxSellLot = 0;
      double sumMid = 0;
      int midCount = 0;

      int total = PositionsTotal();
      for(int i = 0; i < total; i++)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0) continue;
         if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

         double price  = PositionGetDouble(POSITION_PRICE_OPEN);
         double volume = PositionGetDouble(POSITION_VOLUME);
         int    type   = (int)PositionGetInteger(POSITION_TYPE);

         sumMid += price;
         midCount++;

         if(type == POSITION_TYPE_BUY)
         {
            buyCount++;
            if(price > highestBuy)  highestBuy = price;
            if(price < lowestBuy)   lowestBuy  = price;
            if(volume > maxBuyLot)  maxBuyLot  = volume;
         }
         else
         {
            sellCount++;
            if(price > highestSell) highestSell = price;
            if(price < lowestSell)  lowestSell  = price;
            if(volume > maxSellLot) maxSellLot  = volume;
         }
      }

      if(maxBuyLot > maxSellLot)
      {
         g_baskets[b].recoveryDir   = 0;
         g_baskets[b].lastBuyPrice  = lowestBuy;
         g_baskets[b].lastSellPrice = lowestSell;
      }
      else if(maxSellLot > maxBuyLot)
      {
         g_baskets[b].recoveryDir   = 1;
         g_baskets[b].lastBuyPrice  = highestBuy;
         g_baskets[b].lastSellPrice = highestSell;
      }
      else
      {
         g_baskets[b].recoveryDir   = -1;
         g_baskets[b].lastBuyPrice  = highestBuy;
         g_baskets[b].lastSellPrice = lowestSell;
      }

      g_baskets[b].currentLevel  = MathMax(0, (posCount / 2) - 1);
      g_baskets[b].entryMid      = (midCount > 0) ? sumMid / midCount : 0;
      g_baskets[b].active        = true;
      g_baskets[b].positionCount = posCount;
      g_baskets[b].openTime      = TimeCurrent();
   }
}

//+------------------------------------------------------------------+
//| On-chart dashboard (live mode only)                               |
//+------------------------------------------------------------------+
void CreateDashboard()
{
   string prefix = "HG_";
   int x = 10, y = 25, gap = 16;

   ObjectCreate(0, prefix + "bg", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_XDISTANCE, 5);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_YDISTANCE, 20);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_XSIZE, 300);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_YSIZE, 40 + MaxBaskets * gap + 5 * gap);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_BGCOLOR, C'20,20,30');
   ObjectSetInteger(0, prefix + "bg", OBJPROP_BORDER_COLOR, clrDodgerBlue);
   ObjectSetInteger(0, prefix + "bg", OBJPROP_CORNER, CORNER_LEFT_UPPER);

   int totalLabels = 3 + MaxBaskets + 2;
   for(int i = 0; i < totalLabels; i++)
   {
      string name = prefix + "L" + IntegerToString(i);
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
      ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y + i * gap);
      ObjectSetInteger(0, name, OBJPROP_COLOR, clrWhite);
      ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
      ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   }

   ObjectSetString(0, prefix + "L0", OBJPROP_TEXT, "══ HEDGED GRID v2 ══");
   ObjectSetInteger(0, prefix + "L0", OBJPROP_COLOR, clrDodgerBlue);
}

void UpdateDashboard()
{
   if(MQLInfoInteger(MQL_TESTER)) return;

   string prefix = "HG_";
   int active = CountActiveBaskets();
   int totalPos = 0;
   double totalPnL = 0;

   for(int b = 0; b < MaxBaskets; b++)
   {
      if(g_baskets[b].active)
      {
         totalPos += CountPositions(g_baskets[b].magic);
         totalPnL += GetBasketPnL(g_baskets[b].magic);
      }
   }

   ObjectSetString(0, prefix + "L1", OBJPROP_TEXT,
      "Baskets: " + IntegerToString(active) + "/" + IntegerToString(MaxBaskets) +
      "  |  Positions: " + IntegerToString(totalPos));

   color pnlColor = totalPnL >= 0 ? clrLime : clrOrangeRed;
   ObjectSetString(0, prefix + "L2", OBJPROP_TEXT,
      "Float P/L: $" + DoubleToString(totalPnL, 2) +
      "  |  Cycles: " + IntegerToString(g_totalCycles));
   ObjectSetInteger(0, prefix + "L2", OBJPROP_COLOR, pnlColor);

   // Per-basket status
   for(int b = 0; b < MaxBaskets; b++)
   {
      string line;
      if(g_baskets[b].active)
      {
         string dir = g_baskets[b].recoveryDir == 0 ? "BUY" :
                      g_baskets[b].recoveryDir == 1 ? "SELL" : "---";
         double bpnl = GetBasketPnL(g_baskets[b].magic);
         line = "  B#" + IntegerToString(b) +
                " Lv" + IntegerToString(g_baskets[b].currentLevel) +
                " " + dir +
                " $" + DoubleToString(bpnl, 2);
      }
      else
         line = "  B#" + IntegerToString(b) + " [idle]";

      ObjectSetString(0, prefix + "L" + IntegerToString(3 + b), OBJPROP_TEXT, line);
      ObjectSetInteger(0, prefix + "L" + IntegerToString(3 + b), OBJPROP_COLOR,
         g_baskets[b].active ? clrWhite : clrGray);
   }

   int footer = 3 + MaxBaskets;
   ObjectSetString(0, prefix + "L" + IntegerToString(footer), OBJPROP_TEXT,
      "Total P/L: $" + DoubleToString(g_totalProfit, 2) +
      "  |  Trades: " + IntegerToString(g_totalTrades));
   ObjectSetInteger(0, prefix + "L" + IntegerToString(footer), OBJPROP_COLOR, clrGold);

   ObjectSetString(0, prefix + "L" + IntegerToString(footer + 1), OBJPROP_TEXT,
      "DD limit: $" + DoubleToString(MaxDrawdown, 0) +
      "/basket  $" + DoubleToString(MaxTotalDrawdown, 0) + "/total");
   ObjectSetInteger(0, prefix + "L" + IntegerToString(footer + 1), OBJPROP_COLOR, clrDarkGray);

   ChartRedraw();
}
//+------------------------------------------------------------------+
