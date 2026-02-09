package com.example.bg

import android.content.Context
import android.media.MediaPlayer
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path

// =============================
// Retrofit / 資料模型
// =============================

private const val BASE_URL = "https://pt-production.up.railway.app/"

// /api/orders 或 /orders 回傳
data class OrdersResponse(
    val ok: Boolean,
    val orders: List<OrderDto> = emptyList()
)

// 單一訂單
data class OrderDto(
    val id: Long? = null,
    val sessionId: String? = null,
    val tableNo: String? = null,   // 有桌號就留著，不顯示也沒差
    val time: String? = null,
    val status: String? = null,
    val items: List<OrderItemDto>? = null,
    val total: Int? = null,
    val timestamp: Long? = null
)

// 訂單內每一筆品項
data class OrderItemDto(
    val lineId: String? = null,
    val name: String? = null,
    val enName: String? = null,
    val price: Int? = null,
    val qty: Int? = null,
    val remark: String? = null,
    val temp: String? = null,
    val addOns: List<OrderAddonDto>? = null,
    val addedBy: String? = null,
    val category: String? = null      // 前端塞進來的分類：吃堡 / 單點 / 飲品 / 甜點
)

// 加價項
data class OrderAddonDto(
    val key: String? = null,
    val name: String? = null,
    val enName: String? = null,
    val price: Int? = null
)

// 更新狀態用
data class StatusUpdateRequest(
    val status: String
)

data class SimpleResponse(
    val ok: Boolean,
    val msg: String? = null
)

// 叫號：寫入目前叫號的四碼代碼
data class CallUpdateRequest(
    val code: String
)

// =============================
// SoldOut / 庫存管理資料模型
// =============================

data class SoldOutItem(
    val categoryIndex: Int,
    val itemIndex: Int
)

data class SoldOutResponse(
    val ok: Boolean,
    val items: List<List<Int>> = emptyList()
)

data class SoldOutUpdateRequest(
    val items: List<List<Int>>
)

interface OrdersApi {
    // 新版：/api/orders
    @GET("/api/orders")
    suspend fun getOrdersApi(): OrdersResponse

    // 備用：舊版 /orders（如果後端還沒更新就會用到）
    @GET("/orders")
    suspend fun getOrdersLegacy(): OrdersResponse

    // 更新單一訂單狀態：new / making / done / cancelled
    @POST("/api/orders/{id}/status")
    suspend fun updateStatus(
        @Path("id") id: Long,
        @Body body: StatusUpdateRequest
    ): SimpleResponse

    // 精準叫號：把四碼代碼寫入後端目前叫號
    @POST("/api/call")
    suspend fun setCall(
        @Body body: CallUpdateRequest
    ): SimpleResponse

    // 讀取售完品項
    @GET("/soldout")
    suspend fun getSoldOut(): SoldOutResponse

    // 更新售完品項（後端需實作 POST /soldout）
    @POST("/soldout")
    suspend fun updateSoldOut(
        @Body body: SoldOutUpdateRequest
    ): SimpleResponse
}

object OrdersRepository {
    val api: OrdersApi by lazy {
        Retrofit.Builder()
            .baseUrl(BASE_URL)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(OrdersApi::class.java)
    }

    /**
     * 先打 /api/orders，如果 404 再打 /orders
     */
    suspend fun loadOrders(): OrdersResponse {
        return try {
            api.getOrdersApi()
        } catch (e: HttpException) {
            if (e.code() == 404) {
                api.getOrdersLegacy()
            } else {
                throw e
            }
        }
    }

    /**
     * 更新訂單狀態
     */
    suspend fun updateOrderStatus(id: Long, newStatus: String): Boolean {
        return try {
            val res = api.updateStatus(id, StatusUpdateRequest(status = newStatus))
            res.ok
        } catch (e: Exception) {
            false
        }
    }

    /**
     * 精準叫號：把四碼 code 寫到 /api/call
     */
    suspend fun setCurrentCall(code: String): Boolean {
        val c = code.trim()
        // 後端我們做成「必須 4 碼數字」，這裡先擋掉不合法
        if (c.length != 4 || c.any { !it.isDigit() }) return false

        return try {
            val res = api.setCall(CallUpdateRequest(code = c))
            res.ok
        } catch (e: Exception) {
            false
        }
    }

    /**
     * 讀取目前售完清單
     */
    suspend fun loadSoldOut(): List<SoldOutItem> {
        return try {
            val res = api.getSoldOut()
            res.items.mapNotNull { pair ->
                if (pair.size >= 2) {
                    SoldOutItem(
                        categoryIndex = pair[0],
                        itemIndex = pair[1]
                    )
                } else {
                    null
                }
            }
        } catch (e: Exception) {
            emptyList()
        }
    }

    /**
     * 更新售完清單
     */
    suspend fun updateSoldOut(list: List<SoldOutItem>): Boolean {
        return try {
            val body = SoldOutUpdateRequest(
                items = list.map { listOf(it.categoryIndex, it.itemIndex) }
            )
            val res = api.updateSoldOut(body)
            res.ok
        } catch (e: Exception) {
            false
        }
    }
}

// =============================
// 菜單資料（給庫存管理 / SoldOut 用）
// index 必須跟前端 MENU 順序一致
// =============================

data class MenuCategory(
    val index: Int,
    val name: String,
    val items: List<MenuItem>
)

data class MenuItem(
    val index: Int,
    val name: String
)

// 0: 吃堡（Combo）
// 1: 單點（single）
// 2: 飲品（drinks）
// 3: 甜點（desserts）
val MENU_DATA = listOf(
    MenuCategory(
        index = 0,
        name = "吃堡",
        items = listOf(
            MenuItem(0, "經典起司牛肉堡"),
            MenuItem(1, "辣肉醬起司牛肉堡"),
            MenuItem(2, "花生培根起司牛肉堡"),
            MenuItem(3, "燒烤醬培根起司牛肉堡"),
            MenuItem(4, "雙層起司牛肉堡")
        )
    ),
    MenuCategory(
        index = 1,
        name = "單點",
        items = listOf(
            MenuItem(0, "塔塔醬炸雞"),
            MenuItem(1, "番茄莎莎炸雞"),
            MenuItem(2, "辣肉醬炸雞"),
            MenuItem(3, "美式花醬培根炸雞"),
            MenuItem(4, "起司炸薯條"),
            MenuItem(5, "辣肉醬炸薯條"),
            MenuItem(6, "美式炸雞薯餅"),
            MenuItem(7, "洋蔥圈"),
            MenuItem(8, "雞塊"),
            MenuItem(9, "雞米花")
        )
    ),
    MenuCategory(
        index = 2,
        name = "飲品",
        items = listOf(
            MenuItem(0, "紅茶"),
            MenuItem(1, "鮮奶紅茶"),
            MenuItem(2, "冰遇紅茶"),
            MenuItem(3, "可口可樂")
        )
    ),
    MenuCategory(
        index = 3,
        name = "甜點",
        items = listOf(
            MenuItem(0, "OREO 巧克力奶油鬆餅"),
            MenuItem(1, "冰淇淋奶油巧克力鬆餅")
        )
    )
)

// =============================
// 出單邏輯（分類 / 文本組裝）
// =============================

// 三種單：廚房 / 飲料 / 客戶
enum class TicketType {
    KITCHEN,    // 廚房單：吃堡 + 單點
    DRINKS,     // 飲料單：飲品 + 甜點
    CUSTOMER    // 客戶聯：全部 + 價錢
}

// 依種類拆出要印的品項
private fun splitLinesForTicket(order: OrderDto, type: TicketType): List<OrderItemDto> {
    val all = order.items.orEmpty()

    return when (type) {
        TicketType.CUSTOMER -> all

        TicketType.KITCHEN -> all.filter { line ->
            when (line.category) {
                "吃堡", "單點" -> true
                else -> false
            }
        }

        TicketType.DRINKS -> all.filter { line ->
            when (line.category) {
                "飲品", "甜點" -> true
                else -> false
            }
        }
    }
}

// 計算某一品項「含加價」的單價 & 小計
private fun calcLineMoney(line: OrderItemDto): Pair<Int, Int> {
    val base = (line.price ?: 0)
    val add = line.addOns.orEmpty().sumOf { it.price ?: 0 }
    val unit = base + add
    val qty = (line.qty ?: 1).coerceAtLeast(1)
    val lineTotal = unit * qty
    return unit to lineTotal
}

// 溫度顯示
private fun mapTempLabel(temp: String?): String {
    return when (temp) {
        "cold" -> "冰"
        "hot" -> "熱"
        null, "" -> ""
        else -> temp
    }
}

// 狀態中文
private fun statusLabel(status: String?): String {
    return when (status) {
        null, "", "new" -> "存單"
        "making" -> "製作中"
        "done" -> "完成"
        "cancelled" -> "已取消"
        else -> status
    }
}

// 組出實際要印的文字內容
private fun buildTicketText(order: OrderDto, type: TicketType): String {
    val sb = StringBuilder()

    val title = when (type) {
        TicketType.KITCHEN -> "【廚房單】"
        TicketType.DRINKS -> "【飲料單】"
        TicketType.CUSTOMER -> "【客戶聯】"
    }

    // 抬頭：全部都用訂單編號，不顯示桌號
    sb.appendLine("Partner Brunch")
    sb.appendLine(title)
    sb.appendLine("------------------------------")
    sb.appendLine("訂單編號：#${order.id ?: 0}")         // 用 orders.id
    sb.appendLine("代碼：${order.sessionId ?: "--"}")   // 4 碼代碼
    sb.appendLine("時間：${order.time ?: ""}")
    sb.appendLine("------------------------------")

    val lines = splitLinesForTicket(order, type)

    if (lines.isEmpty()) {
        sb.appendLine("(本張單沒有對應品項)")
    } else {
        val showPrice = (type == TicketType.CUSTOMER)   // 僅客戶單顯示價錢

        for (line in lines) {
            val name = line.name.orEmpty()
            val qty = (line.qty ?: 1).coerceAtLeast(1)
            val tempLabel = mapTempLabel(line.temp)
            val addonsText = line.addOns
                ?.takeIf { it.isNotEmpty() }
                ?.joinToString("、") { it.name.orEmpty() }
                ?: ""
            val remark = line.remark.orEmpty()

            if (showPrice) {
                val (_, lineTotal) = calcLineMoney(line)
                sb.appendLine("$name x$qty  $lineTotal 元")
            } else {
                // 廚房單 / 飲料單：不顯示價格
                sb.appendLine("$name x$qty")
            }

            if (tempLabel.isNotEmpty()) {
                sb.appendLine("  溫度：$tempLabel")
            }
            if (addonsText.isNotEmpty()) {
                sb.appendLine("  加價：$addonsText")
            }
            if (remark.isNotBlank()) {
                sb.appendLine("  備註：$remark")
            }
        }
    }

    if (type == TicketType.CUSTOMER) {
        sb.appendLine("------------------------------")
        sb.appendLine("合計：${order.total ?: 0} 元")
    }

    sb.appendLine()
    return sb.toString()
}

// 目前先「模擬列印」，之後你可以接 USB 印表機邏輯
private fun printTicket(
    context: Context,
    order: OrderDto,
    type: TicketType
) {
    val text = buildTicketText(order, type)
    Log.d("TicketPrint", "\n$text")

    val msg = when (type) {
        TicketType.KITCHEN -> "已送出廚房單"
        TicketType.DRINKS -> "已送出飲料單"
        TicketType.CUSTOMER -> "已送出客戶聯"
    }
    Toast.makeText(context, msg, Toast.LENGTH_SHORT).show()
}

// 一次出三張單
private fun printAllTickets(context: Context, order: OrderDto) {
    printTicket(context, order, TicketType.KITCHEN)
    printTicket(context, order, TicketType.DRINKS)
    printTicket(context, order, TicketType.CUSTOMER)
}

// =============================
// 新訂單提示音（提醒聲約 3 秒）
// =============================

private fun playNewOrderSound(context: Context) {
    // 請在 res/raw 底下放一個 new_order.mp3（或 wav），長度大約 3 秒
    // 檔名要叫 new_order（不要有大寫）
    try {
        val mp = MediaPlayer.create(context, R.raw.new_order)
        mp?.setOnCompletionListener { it.release() }
        mp?.start()
    } catch (e: Exception) {
        Log.e("OrderSound", "播放提示音失敗: ${e.message}")
    }
}

// =============================
// Activity / Composables
// =============================

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            MaterialTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    OrdersScreen()
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun OrdersScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var orders by remember { mutableStateOf<List<OrderDto>>(emptyList()) }
    var isLoading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    // 放大明細用
    var detailOrder by remember { mutableStateOf<OrderDto?>(null) }
    var showDetail by remember { mutableStateOf(false) }
    var detailAction by remember {
        mutableStateOf<((OrderDto, List<OrderItemDto>) -> Unit)?>(null)
    }

    // tab：0 = 製作面板（存單 + 製作中 + 工具），1 = 訂單總覽（完成區）
    var currentTab by remember { mutableStateOf(0) }

    // 自動偵測新訂單 → 提示音
    var lastMaxOrderId by remember { mutableStateOf<Long?>(null) }

    // 庫存管理 / soldout
    var showStockDialog by remember { mutableStateOf(false) }
    var soldOutItems by remember { mutableStateOf<List<SoldOutItem>>(emptyList()) }
    var isStockLoading by remember { mutableStateOf(false) }
    var isStockUpdating by remember { mutableStateOf(false) }

    fun reload() {
        if (isLoading) return
        scope.launch {
            isLoading = true
            error = null
            try {
                val res = withContext(Dispatchers.IO) {
                    OrdersRepository.loadOrders()
                }
                if (!res.ok) {
                    error = "後端回傳 ok=false"
                }
                orders = res.orders.sortedByDescending { it.id ?: 0L }
            } catch (e: Exception) {
                error = e.message ?: "載入失敗"
            } finally {
                isLoading = false
            }
        }
    }

    fun changeStatus(order: OrderDto, newStatus: String) {
        val id = order.id ?: return
        scope.launch {
            val success = withContext(Dispatchers.IO) {
                OrdersRepository.updateOrderStatus(id, newStatus)
            }
            if (success) {
                orders = orders.map {
                    if (it.id == id) it.copy(status = newStatus) else it
                }

                // 完成 → 叫號
                if (newStatus == "done") {
                    val code = order.sessionId.orEmpty()
                    val ok = withContext(Dispatchers.IO) {
                        OrdersRepository.setCurrentCall(code)
                    }
                    if (!ok) {
                        Toast.makeText(context, "叫號更新失敗（代碼需 4 碼數字）", Toast.LENGTH_SHORT).show()
                    } else {
                        Toast.makeText(context, "已叫號：$code", Toast.LENGTH_SHORT).show()
                    }
                }

            } else {
                Toast.makeText(context, "更新狀態失敗", Toast.LENGTH_SHORT).show()
            }
        }
    }

    // 初次載入訂單
    LaunchedEffect(Unit) {
        reload()
    }

    // 每 3 秒自動刷新一次訂單
    LaunchedEffect(Unit) {
        while (true) {
            delay(3000)
            reload()
        }
    }

    // 偵測新訂單 → 播放提示音
    LaunchedEffect(orders) {
        val currentMax = orders.maxOfOrNull { it.id ?: 0L }
        if (currentMax != null) {
            val last = lastMaxOrderId
            if (last != null && currentMax > last) {
                // 有新訂單進來
                playNewOrderSound(context)
            }
            lastMaxOrderId = currentMax
        }
    }

    // 初次載入 soldout
    LaunchedEffect(Unit) {
        try {
            val list = withContext(Dispatchers.IO) {
                OrdersRepository.loadSoldOut()
            }
            soldOutItems = list
        } catch (e: Exception) {
            Log.e("OrdersScreen", "loadSoldOut failed: ${e.message}")
        }
    }

    // 依狀態分三區
    val savedList = orders.filter { it.status.isNullOrBlank() || it.status == "new" }
    val makingList = orders.filter { it.status == "making" }
    val doneList = orders.filter { it.status == "done" }

    Scaffold(
        topBar = {
            CenterAlignedTopAppBar(
                title = {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            text = "Partner 出單控制台",
                            fontWeight = FontWeight.Bold,
                            fontSize = 20.sp
                        )
                        Text(
                            text = "存單 → 製作 → 完成 / 叫號",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.outline
                        )
                    }
                }
            )
        }
    ) { paddingValues ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(paddingValues)
                .padding(12.dp)
        ) {
            // Tab 切換：製作面板 / 訂單總覽
            ScrollableTabRow(
                selectedTabIndex = currentTab,
                edgePadding = 0.dp,
                divider = {}
            ) {
                Tab(
                    selected = currentTab == 0,
                    onClick = { currentTab = 0 },
                    text = { Text("製作面板", fontWeight = FontWeight.SemiBold) }
                )
                Tab(
                    selected = currentTab == 1,
                    onClick = { currentTab = 1 },
                    text = { Text("訂單總覽（完成）", fontWeight = FontWeight.SemiBold) }
                )
            }

            if (isLoading) {
                LinearProgressIndicator(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 4.dp)
                )
            }

            if (error != null) {
                Text(
                    text = "錯誤：$error",
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 8.dp)
                )
            }

            Spacer(modifier = Modifier.height(6.dp))

            if (orders.isEmpty() && !isLoading && error == null) {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(24.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        text = "目前沒有訂單",
                        color = MaterialTheme.colorScheme.outline,
                        fontWeight = FontWeight.Bold
                    )
                }
            } else {
                when (currentTab) {
                    0 -> {
                        // ========= 製作面板：三個格子 =========
                        Row(
                            modifier = Modifier
                                .fillMaxSize()
                                .padding(top = 4.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            // 1. 存單區（客戶剛點完 / 尚未出單）
                            Column(
                                modifier = Modifier
                                    .weight(1f)
                                    .fillMaxHeight()
                            ) {
                                SectionHeader(
                                    title = "存單區",
                                    subtitle = "客戶剛點完，尚未出單",
                                    badge = "${savedList.size} 筆"
                                )
                                Surface(
                                    modifier = Modifier
                                        .fillMaxSize(),
                                    tonalElevation = 2.dp,
                                    shape = MaterialTheme.shapes.medium
                                ) {
                                    if (savedList.isEmpty()) {
                                        EmptyHint("目前沒有存單")
                                    } else {
                                        LazyColumn(
                                            modifier = Modifier
                                                .fillMaxSize()
                                                .padding(8.dp),
                                            verticalArrangement = Arrangement.spacedBy(8.dp)
                                        ) {
                                            items(savedList) { order ->
                                                OrderCard(
                                                    order = order,
                                                    primaryLabel = "出單 → 移到製作區",
                                                    onPrimaryClick = {
                                                        // 出三張單 + 狀態改 making
                                                        printAllTickets(context, order)
                                                        changeStatus(order, "making")
                                                    },
                                                    secondaryLabel = "放大修改再出單",
                                                    onSecondaryClick = {
                                                        detailOrder = order
                                                        detailAction = { o, items ->
                                                            val newOrder = o.copy(items = items)
                                                            printAllTickets(context, newOrder)
                                                            changeStatus(o, "making")
                                                        }
                                                        showDetail = true
                                                    }
                                                )
                                            }
                                        }
                                    }
                                }
                            }

                            // 2. 製作區（已出單，正在做）
                            Column(
                                modifier = Modifier
                                    .weight(1f)
                                    .fillMaxHeight()
                            ) {
                                SectionHeader(
                                    title = "製作區",
                                    subtitle = "廚房 / 飲料 正在製作",
                                    badge = "${makingList.size} 筆"
                                )
                                Surface(
                                    modifier = Modifier
                                        .fillMaxSize(),
                                    tonalElevation = 2.dp,
                                    shape = MaterialTheme.shapes.medium
                                ) {
                                    if (makingList.isEmpty()) {
                                        EmptyHint("目前沒有製作中的訂單")
                                    } else {
                                        LazyColumn(
                                            modifier = Modifier
                                                .fillMaxSize()
                                                .padding(8.dp),
                                            verticalArrangement = Arrangement.spacedBy(8.dp)
                                        ) {
                                            items(makingList) { order ->
                                                OrderCard(
                                                    order = order,
                                                    primaryLabel = "完成 → 移到完成區",
                                                    onPrimaryClick = {
                                                        // 完成 → 狀態 done + 叫號
                                                        changeStatus(order, "done")
                                                    },
                                                    secondaryLabel = "重印全部",
                                                    onSecondaryClick = {
                                                        printAllTickets(context, order)
                                                    }
                                                )
                                            }
                                        }
                                    }
                                }
                            }

                            // 3. 功能 / 概況區（比較窄）
                            Column(
                                modifier = Modifier
                                    .weight(0.8f)
                                    .fillMaxHeight()
                            ) {
                                SectionHeader(
                                    title = "狀態 / 工具",
                                    subtitle = "概況 + 快捷操作",
                                    badge = ""
                                )

                                Surface(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .weight(1f),
                                    tonalElevation = 2.dp,
                                    shape = MaterialTheme.shapes.medium
                                ) {
                                    Column(
                                        modifier = Modifier
                                            .fillMaxSize()
                                            .padding(12.dp),
                                        verticalArrangement = Arrangement.spacedBy(12.dp)
                                    ) {
                                        Text(
                                            text = "當前概況",
                                            fontWeight = FontWeight.SemiBold,
                                            fontSize = 14.sp
                                        )
                                        Text(
                                            text = "存單：${savedList.size} 筆\n" +
                                                    "製作中：${makingList.size} 筆\n" +
                                                    "完成（今日內）約：${doneList.size} 筆",
                                            fontSize = 13.sp,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant
                                        )

                                        Divider(modifier = Modifier.padding(vertical = 4.dp))

                                        Text(
                                            text = "快速操作",
                                            fontWeight = FontWeight.SemiBold,
                                            fontSize = 14.sp
                                        )

                                        Button(
                                            modifier = Modifier.fillMaxWidth(),
                                            onClick = { reload() }
                                        ) {
                                            Text("立即重新整理", fontSize = 13.sp)
                                        }

                                        OutlinedButton(
                                            modifier = Modifier.fillMaxWidth(),
                                            onClick = {
                                                if (isStockLoading) return@OutlinedButton
                                                scope.launch {
                                                    isStockLoading = true
                                                    try {
                                                        val list = withContext(Dispatchers.IO) {
                                                            OrdersRepository.loadSoldOut()
                                                        }
                                                        soldOutItems = list
                                                        showStockDialog = true
                                                    } catch (e: Exception) {
                                                        Toast
                                                            .makeText(
                                                                context,
                                                                "讀取庫存資料失敗",
                                                                Toast.LENGTH_SHORT
                                                            )
                                                            .show()
                                                    } finally {
                                                        isStockLoading = false
                                                    }
                                                }
                                            }
                                        ) {
                                            Text(
                                                text = if (isStockLoading) "庫存讀取中…" else "庫存管理",
                                                fontSize = 13.sp
                                            )
                                        }

                                        Spacer(modifier = Modifier.weight(1f))

                                        Text(
                                            text = "※ 目前已啟用：\n" +
                                                    "• 每 3 秒自動刷新訂單\n" +
                                                    "• 新訂單提示音\n" +
                                                    "• SoldOut 庫存管理（與前端同步）",
                                            fontSize = 11.sp,
                                            color = MaterialTheme.colorScheme.outline
                                        )
                                    }
                                }
                            }
                        }
                    }

                    1 -> {
                        // ========= 訂單總覽（完成區） =========
                        SectionHeader(
                            title = "完成訂單",
                            subtitle = "歷史完成單，可查詢 / 重印",
                            badge = "${doneList.size} 筆"
                        )
                        Surface(
                            modifier = Modifier
                                .fillMaxSize(),
                            tonalElevation = 2.dp,
                            shape = MaterialTheme.shapes.medium
                        ) {
                            if (doneList.isEmpty()) {
                                EmptyHint("目前沒有完成的訂單")
                            } else {
                                LazyColumn(
                                    modifier = Modifier
                                        .fillMaxSize()
                                        .padding(8.dp),
                                    verticalArrangement = Arrangement.spacedBy(8.dp)
                                ) {
                                    items(doneList) { order ->
                                        OrderCard(
                                            order = order,
                                            primaryLabel = "重印客戶聯",
                                            onPrimaryClick = {
                                                printTicket(context, order, TicketType.CUSTOMER)
                                            },
                                            secondaryLabel = null,
                                            onSecondaryClick = null
                                        )
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // 放大修改 dialog
    if (showDetail && detailOrder != null && detailAction != null) {
        OrderDetailDialog(
            order = detailOrder!!,
            onDismiss = { showDetail = false },
            onPrint = { modifiedItems ->
                detailAction?.invoke(detailOrder!!, modifiedItems)
                showDetail = false
            }
        )
    }

    // 庫存管理 dialog
    if (showStockDialog) {
        StockDialog(
            items = soldOutItems,
            onUpdate = { newList ->
                scope.launch {
                    if (isStockUpdating) return@launch
                    isStockUpdating = true
                    val ok = withContext(Dispatchers.IO) {
                        OrdersRepository.updateSoldOut(newList)
                    }
                    isStockUpdating = false
                    if (ok) {
                        soldOutItems = newList
                        Toast.makeText(
                            context,
                            "已更新售完品項",
                            Toast.LENGTH_SHORT
                        ).show()
                        showStockDialog = false
                    } else {
                        Toast.makeText(
                            context,
                            "更新售完清單失敗",
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                }
            },
            onDismiss = { showStockDialog = false }
        )
    }
}

// =============================
// 區塊標題 / 空畫面
// =============================

@Composable
fun SectionHeader(
    title: String,
    subtitle: String,
    badge: String
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = title,
                fontWeight = FontWeight.Bold,
                fontSize = 16.sp
            )
            if (subtitle.isNotEmpty()) {
                Text(
                    text = subtitle,
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.outline
                )
            }
        }
        if (badge.isNotEmpty()) {
            Box(
                modifier = Modifier
                    .background(
                        color = MaterialTheme.colorScheme.primary.copy(alpha = 0.12f),
                        shape = MaterialTheme.shapes.small
                    )
                    .padding(horizontal = 8.dp, vertical = 4.dp),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = badge,
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.primary,
                    fontWeight = FontWeight.SemiBold
                )
            }
        }
    }
}

@Composable
fun EmptyHint(text: String) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .padding(12.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = text,
            color = MaterialTheme.colorScheme.outline,
            fontSize = 13.sp
        )
    }
}

// =============================
// 單張訂單卡片（共用）
// =============================

@Composable
fun OrderCard(
    order: OrderDto,
    primaryLabel: String,
    onPrimaryClick: () -> Unit,
    secondaryLabel: String? = null,
    onSecondaryClick: (() -> Unit)? = null
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        elevation = CardDefaults.cardElevation(4.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.9f)
        )
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = "訂單 #${order.id ?: 0}",
                        fontWeight = FontWeight.Bold,
                        fontSize = 18.sp
                    )
                    Text(
                        text = "代碼：${order.sessionId ?: "--"}",
                        fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.outline
                    )
                    Text(
                        text = "時間：${order.time ?: "--"}",
                        fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.outline
                    )
                }
                Column(horizontalAlignment = Alignment.End) {
                    Text(
                        text = statusLabel(order.status),
                        fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.primary,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "品項數：${order.items?.size ?: 0}",
                        fontSize = 13.sp
                    )
                    Text(
                        text = "總額：${order.total ?: 0} 元",
                        fontSize = 13.sp
                    )
                }
            }

            Spacer(modifier = Modifier.height(8.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Button(
                    modifier = Modifier.weight(1f),
                    onClick = onPrimaryClick
                ) {
                    Text(
                        text = primaryLabel,
                        fontSize = 13.sp,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis
                    )
                }

                if (secondaryLabel != null && onSecondaryClick != null) {
                    OutlinedButton(
                        modifier = Modifier.weight(1f),
                        onClick = onSecondaryClick
                    ) {
                        Text(
                            text = secondaryLabel,
                            fontSize = 13.sp,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis
                        )
                    }
                }
            }
        }
    }
}

// =============================
// 放大明細 Dialog（可調整數量）
// =============================

@Composable
fun OrderDetailDialog(
    order: OrderDto,
    onDismiss: () -> Unit,
    onPrint: (List<OrderItemDto>) -> Unit
) {
    val itemsState = remember(order) {
        mutableStateListOf<OrderItemDto>().apply {
            addAll(order.items.orEmpty())
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(
                onClick = {
                    onPrint(itemsState.toList())
                }
            ) {
                Text("出單")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("取消")
            }
        },
        title = {
            Text(
                text = "訂單 #${order.id ?: 0}",
                fontWeight = FontWeight.Bold
            )
        },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 0.dp, max = 420.dp)
            ) {
                Text(
                    text = "時間：${order.time ?: "--"}",
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.outline
                )
                Spacer(modifier = Modifier.height(8.dp))

                if (itemsState.isEmpty()) {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(24.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            text = "沒有明細",
                            color = MaterialTheme.colorScheme.outline
                        )
                    }
                } else {
                    LazyColumn(
                        modifier = Modifier
                            .fillMaxWidth()
                            .weight(1f, fill = false),
                        verticalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        items(itemsState, key = { it.lineId ?: it.name ?: "" }) { line ->
                            OrderLineEditor(
                                item = line,
                                onChange = { newItem ->
                                    val idx = itemsState.indexOfFirst { it.lineId == line.lineId }
                                    if (idx >= 0) {
                                        itemsState[idx] = newItem
                                    }
                                }
                            )
                        }
                    }
                }
            }
        }
    )
}

@Composable
fun OrderLineEditor(
    item: OrderItemDto,
    onChange: (OrderItemDto) -> Unit
) {
    val qty = (item.qty ?: 1).coerceAtLeast(1)
    val category = item.category ?: ""

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.55f)
        )
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(8.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = item.name.orEmpty(),
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = "分類：$category",
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.outline
                    )
                }

                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedButton(
                        onClick = {
                            val newQty = (qty - 1).coerceAtLeast(1)
                            onChange(item.copy(qty = newQty))
                        },
                        contentPadding = PaddingValues(horizontal = 6.dp, vertical = 0.dp)
                    ) { Text("-", fontSize = 16.sp) }

                    Text(
                        text = qty.toString(),
                        modifier = Modifier
                            .width(32.dp)
                            .padding(horizontal = 4.dp),
                        textAlign = TextAlign.Center
                    )

                    OutlinedButton(
                        onClick = {
                            val newQty = qty + 1
                            onChange(item.copy(qty = newQty))
                        },
                        contentPadding = PaddingValues(horizontal = 6.dp, vertical = 0.dp)
                    ) { Text("+", fontSize = 16.sp) }
                }
            }

            val tempLabel = mapTempLabel(item.temp)
            val addonsText = item.addOns
                ?.takeIf { it.isNotEmpty() }
                ?.joinToString("、") { it.name.orEmpty() }
                ?: ""
            val remark = item.remark.orEmpty()
            val added = item.addedBy.orEmpty()

            if (tempLabel.isNotEmpty()) {
                Text(
                    text = "溫度：$tempLabel",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.outline
                )
            }
            if (addonsText.isNotEmpty()) {
                Text(
                    text = "加價：$addonsText",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.outline
                )
            }
            if (remark.isNotEmpty()) {
                Text(
                    text = "備註：$remark",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.outline
                )
            }
            if (added.isNotEmpty()) {
                Text(
                    text = "點餐者：$added",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.outline
                )
            }
        }
    }
}

// =============================
// 庫存管理 Dialog（SoldOut）
// =============================

@Composable
fun StockDialog(
    items: List<SoldOutItem>,
    onUpdate: (List<SoldOutItem>) -> Unit,
    onDismiss: () -> Unit
) {
    var localList by remember(items) {
        mutableStateOf(
            items.sortedWith(compareBy({ it.categoryIndex }, { it.itemIndex }))
        )
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(
                onClick = {
                    onUpdate(localList)
                }
            ) {
                Text("儲存")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("關閉")
            }
        },
        title = {
            Text(
                text = "庫存管理（售完）",
                fontWeight = FontWeight.Bold
            )
        },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 0.dp, max = 420.dp)
            ) {
                Text(
                    text = "勾選的品項會標記為「售完」，前端點餐頁會自動鎖住。",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.outline
                )

                Spacer(modifier = Modifier.height(8.dp))

                LazyColumn(
                    modifier = Modifier
                        .fillMaxWidth()
                        .weight(1f, fill = false),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    items(MENU_DATA, key = { it.index }) { cat ->
                        Card(
                            modifier = Modifier.fillMaxWidth(),
                            colors = CardDefaults.cardColors(
                                containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(
                                    alpha = 0.6f
                                )
                            )
                        ) {
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(8.dp)
                            ) {
                                Text(
                                    text = "${cat.name}（分類 index = ${cat.index}）",
                                    fontSize = 13.sp,
                                    fontWeight = FontWeight.SemiBold
                                )

                                Spacer(modifier = Modifier.height(4.dp))

                                cat.items.forEach { mi ->
                                    val checked = localList.any {
                                        it.categoryIndex == cat.index &&
                                                it.itemIndex == mi.index
                                    }

                                    Row(
                                        modifier = Modifier
                                            .fillMaxWidth()
                                            .padding(vertical = 2.dp),
                                        verticalAlignment = Alignment.CenterVertically
                                    ) {
                                        Column(
                                            modifier = Modifier.weight(1f)
                                        ) {
                                            Text(
                                                text = mi.name,
                                                fontSize = 13.sp
                                            )
                                            Text(
                                                text = "index：(${cat.index}, ${mi.index})",
                                                fontSize = 11.sp,
                                                color = MaterialTheme.colorScheme.outline
                                            )
                                        }

                                        Checkbox(
                                            checked = checked,
                                            onCheckedChange = { isChecked ->
                                                localList =
                                                    if (isChecked) {
                                                        val target = SoldOutItem(
                                                            categoryIndex = cat.index,
                                                            itemIndex = mi.index
                                                        )
                                                        if (localList.any {
                                                                it.categoryIndex == cat.index &&
                                                                        it.itemIndex == mi.index
                                                            }
                                                        ) {
                                                            localList
                                                        } else {
                                                            (localList + target).sortedWith(
                                                                compareBy(
                                                                    { it.categoryIndex },
                                                                    { it.itemIndex }
                                                                )
                                                            )
                                                        }
                                                    } else {
                                                        localList.filterNot {
                                                            it.categoryIndex == cat.index &&
                                                                    it.itemIndex == mi.index
                                                        }
                                                    }
                                            }
                                        )
                                    }
                                }
                            }
                        }
                    }
                }

                Spacer(modifier = Modifier.height(4.dp))

                Text(
                    text = "※ index 會直接寫入 /soldout，與前端共享。",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.outline
                )
            }
        }
    )
}
