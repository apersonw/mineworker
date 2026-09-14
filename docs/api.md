# API

::: netspy.AirSpider
    options:
      members: [start, stop]

::: netspy.Spider
    options:
      members: [start, stop]

::: netspy.TaskSpider
    options:
      members: [task_requests, fetch_tasks, add_tasks, push_tasks, start, stop]

::: netspy.BatchSpider
    options:
      members: [task_requests, update_task, failed_request, start, start_monitor, stop]

::: netspy.core.batch_store.BatchStore

::: netspy.core.batch_monitor.BatchMonitor
    options:
      members: [run, run_once, stop]

::: netspy.BaseParser

::: netspy.Request

::: netspy.Response

::: netspy.Item

::: netspy.UpdateItem

::: netspy.pipelines.base.BasePipeline

::: netspy.pipelines.mysql.MysqlPipeline

::: netspy.db.mysqldb.MysqlDB

::: netspy.User

::: netspy.network.user_pool.base.UserPool

::: netspy.LocalUserPool

::: netspy.GuestUserPool

::: netspy.RedisUserPool

::: netspy.network.middleware.DownloaderMiddleware

::: netspy.network.proxy_pool.base.ProxyPool
