from __future__ import annotations

from mineworker import Item, UpdateItem


class NewsItem(Item):
    pass


class ProductDetailItem(Item):
    __unique_key__ = ["sku"]


def test_table_name_derived_from_class() -> None:
    assert NewsItem().table_name == "news"
    assert ProductDetailItem().table_name == "product_detail"


def test_table_name_explicit_and_instance_override() -> None:
    class Fixed(Item):
        __table_name__ = "hard_coded"

    assert Fixed().table_name == "hard_coded"

    it = NewsItem()
    it.table_name = "custom"
    assert it.table_name == "custom"


def test_fields_via_kwargs_and_attributes() -> None:
    it = NewsItem(title="t", url="u")
    it.author = "a"
    assert it.to_dict() == {"title": "t", "url": "u", "author": "a"}


def test_to_dict_excludes_private_and_callables() -> None:
    it = NewsItem(title="t")
    it._scratch = "x"
    it.helper = lambda: 1
    assert it.to_dict() == {"title": "t"}


def test_fingerprint_uses_unique_key_only() -> None:
    a = ProductDetailItem(sku="A1", price=10, seen_at="monday")
    b = ProductDetailItem(sku="A1", price=99, seen_at="tuesday")
    c = ProductDetailItem(sku="A2", price=10)
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


def test_fingerprint_all_fields_when_no_unique_key() -> None:
    a = NewsItem(title="t", url="u")
    b = NewsItem(url="u", title="t")
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != NewsItem(title="t", url="v").fingerprint


def test_fingerprint_differs_by_table() -> None:
    a = NewsItem(title="t")
    b = NewsItem(title="t")
    b.table_name = "other"
    assert a.fingerprint != b.fingerprint


def test_pre_to_db_hook_runs() -> None:
    class Cleaned(Item):
        def pre_to_db(self) -> None:
            self.title = self.title.strip()

    it = Cleaned(title="  hi  ")
    it.pre_to_db()
    assert it.title == "hi"


def test_update_item_keys() -> None:
    class PriceItem(UpdateItem):
        __update_key__ = ["sku"]

    it = PriceItem(sku="X", price=5)
    assert it.update_key == ["sku"]
    assert isinstance(it, Item)


def test_update_item_falls_back_to_unique_key() -> None:
    class P(UpdateItem):
        __unique_key__ = ["a", "b"]

    assert P(a=1, b=2).update_key == ["a", "b"]


def test_per_item_pipelines_override() -> None:
    it = NewsItem(title="t")
    assert it.pipelines is None
    it.pipelines = ["x.Y"]
    assert it.pipelines == ["x.Y"]

    class WithClassPipes(Item):
        __pipelines__ = ["a.B"]

    assert WithClassPipes().pipelines == ["a.B"]


def test_repr_is_readable() -> None:
    assert "news" in repr(NewsItem(title="t"))


def test_unique_key_all_empty_falls_back_to_all_fields() -> None:
    it = ProductDetailItem(other="v")  # sku 缺失
    assert it.fingerprint == ProductDetailItem(other="v").fingerprint
    assert it.fingerprint != ProductDetailItem(other="w").fingerprint
