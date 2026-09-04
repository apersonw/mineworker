# API

::: mineworker.AirSpider
    options:
      members: [start, stop]

::: mineworker.Spider
    options:
      members: [start, stop]

::: mineworker.TaskSpider
    options:
      members: [task_requests, fetch_tasks, add_tasks, push_tasks, start, stop]

::: mineworker.BatchSpider
    options:
      members: [task_requests, update_task, failed_request, start, start_monitor, stop]

::: mineworker.core.batch_store.BatchStore

::: mineworker.core.batch_monitor.BatchMonitor
    options:
      members: [run, run_once, stop]

::: mineworker.BaseParser

::: mineworker.Request

::: mineworker.Response

::: mineworker.Item

::: mineworker.UpdateItem

::: mineworker.pipelines.base.BasePipeline

::: mineworker.pipelines.mysql.MysqlPipeline

::: mineworker.db.mysqldb.MysqlDB

::: mineworker.User

::: mineworker.network.user_pool.base.UserPool

::: mineworker.LocalUserPool

::: mineworker.GuestUserPool

::: mineworker.RedisUserPool

::: mineworker.network.middleware.DownloaderMiddleware

::: mineworker.network.proxy_pool.base.ProxyPool
