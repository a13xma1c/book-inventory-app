from bookinventory.database import Book, Database
from bookinventory.lookup import find_book
from bookinventory.matching import candidate_queries, is_match, match_score
from bookinventory.scanner import identify_books


def test_candidate_queries_filters_noise_and_dupes():
    lines = ["Dune", "dune", "  Frank   Herbert ", "12", "$$ 4 5 ##", "ab", "978-0-441"]
    assert candidate_queries(lines) == ["Dune", "Frank Herbert"]


def test_match_score():
    assert match_score("Dune Herbert", "Dune", "Frank Herbert") == 1.0
    assert is_match("the hobbit", "The Hobbit", "J.R.R. Tolkien")
    assert not is_match("Cooking Basics", "Quantum Field Theory", "A. Zee")


def test_find_book_picks_matching_doc():
    def fetch(query):
        return {
            "docs": [
                {"title": "Unrelated Thing", "author_name": ["Someone"]},
                {
                    "title": "Dune",
                    "author_name": ["Frank Herbert"],
                    "first_publish_year": 1965,
                    "isbn": ["9780441172719"],
                },
            ]
        }

    book = find_book("Dune Herbert", fetch=fetch)
    assert (book.title, book.authors, book.year, book.isbn) == (
        "Dune", "Frank Herbert", "1965", "9780441172719",
    )


def test_find_book_handles_network_error_and_no_match():
    def broken(query):
        raise OSError("offline")

    assert find_book("Dune", fetch=broken) is None
    assert find_book("Dune", fetch=lambda q: {"docs": []}) is None


def test_identify_books_dedupes():
    dune = Book(title="Dune", authors="Frank Herbert")
    found = identify_books(["Dune", "Frank Herbert", "zzzz junk"], find=lambda q: dune if q != "zzzz junk" else None)
    assert found == [dune]


def test_database_add_list_delete():
    db = Database()
    assert db.add(Book(title="Dune", authors="Frank Herbert"))
    assert not db.add(Book(title="DUNE", authors="frank herbert"))  # case-insensitive dupe
    assert db.add(Book(title="Emma", authors="Jane Austen"))
    assert [b.title for b in db.all()] == ["Dune", "Emma"]
    db.delete(db.all()[0].id)
    assert db.count() == 1
